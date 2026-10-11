"""Keeping an account's repository in step with its Profile Room.

- A message from the room's owner becomes an ``app.bsky.feed.post``: text
  as text, an image or MP4 video as native media, any other file as a link
  card. Text longer than one post becomes a thread.
- Redacting that message deletes everything made from it.
- Changing the room's name, topic, avatar or banner updates the profile.

Nobody else's messages in the room are ever published, and nothing here
stores what was said: each record is written with a source pointing back
at the Matrix event or room it came from (see ``bridge.matrix_records``).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import Request

from bridge.atproto import cbor
from bridge.atproto.cid import CID
from bridge.commands import bot_mxid, message_addresses_bot, notice
from bridge.matrix_records import (
    EXTERNAL_EMBED_TYPE,
    MAX_THREAD_POSTS,
    MEDIA_MSGTYPES,
    POST_COLLECTION,
    media_filename,
    post_parts,
    post_record_v1,
    post_source,
)
from bridge.media import VIDEO_MAX_BYTES, VIDEO_MIME_TYPE, format_size, prepare_image
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
from bridge.pds import PdsError, Write
from bridge.post_lookup import PostRef
from bridge.profile_sync import sync_profile_record
from bridge.repository import Account, BlobRow, PublishedMedia
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# Bookkeeping that ties a repost's Profile Room card to the event that
# caused it, in both directions (see bridge.interactions.send_repost).
EVENT_CARD_KEY = "event_card"
CARD_SOURCE_KEY = "card_source"


def _aspect_ratio(width: Any, height: Any) -> dict[str, int] | None:
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return {"width": width, "height": height}
    return None


def _blob_ref(cid: CID, mime_type: str, size: int) -> dict[str, Any]:
    return {"$type": "blob", "ref": cid, "mimeType": mime_type, "size": size}


async def _build_embed(request: Request, account: Account, event: dict) -> dict[str, Any] | None:
    """The embed for a media message, with whatever it refers to (a blob,
    a published file) recorded. ``None`` if the media can't be used, in
    which case the owner has been told why."""
    config = request.app.state.config
    synapse = request.app.state.synapse
    repository = request.app.state.repository
    room_id = event["room_id"]
    event_id = event["event_id"]
    content = event.get("content") or {}
    mxc = content.get("url")
    if not isinstance(mxc, str) or "file" in content:
        await notice(request, room_id, "That attachment is encrypted, so I can't publish it.")
        return None
    try:
        download = await synapse.download_media(mxc)
    except SynapseError:
        logger.warning("Could not download %s", mxc, exc_info=True)
        download = None
    if download is None:
        await notice(request, room_id, "I couldn't fetch that attachment, so it wasn't published.")
        return None
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    mime_type = (info.get("mimetype") or download.content_type).split(";", 1)[0].strip().lower()
    filename = media_filename(event)
    data = download.content

    if content.get("msgtype") == "m.image" or mime_type.startswith("image/"):
        image = await prepare_image(data)
        if image is not None:
            if image.reencoded:
                try:
                    mxc = await synapse.upload_media(
                        image.data, image.mime_type, filename, as_user_id=bot_mxid(config)
                    )
                except SynapseError:
                    logger.warning("Could not store re-encoded image for %s", event_id, exc_info=True)
                    await notice(request, room_id, "I couldn't prepare that image, so it wasn't published.")
                    return None
            cid = CID.for_raw(image.data)
            await repository.add_blob(BlobRow(account.did, str(cid), mxc, image.mime_type, len(image.data), event_id))
            entry: dict[str, Any] = {"alt": "", "image": _blob_ref(cid, image.mime_type, len(image.data))}
            ratio = _aspect_ratio(image.width, image.height)
            if ratio:
                entry["aspectRatio"] = ratio
            return {"$type": "app.bsky.embed.images", "images": [entry]}
        # Not decodable as an image after all: fall through to a link.

    if content.get("msgtype") == "m.video" and mime_type == VIDEO_MIME_TYPE and len(data) <= VIDEO_MAX_BYTES:
        cid = CID.for_raw(data)
        await repository.add_blob(BlobRow(account.did, str(cid), mxc, VIDEO_MIME_TYPE, len(data), event_id))
        embed: dict[str, Any] = {"$type": "app.bsky.embed.video", "video": _blob_ref(cid, VIDEO_MIME_TYPE, len(data))}
        ratio = _aspect_ratio(info.get("w"), info.get("h"))
        if ratio:
            embed["aspectRatio"] = ratio
        return embed

    # No native form: link to the file, served by the bridge itself. The
    # card's title (the filename) is filled in from the event whenever the
    # record is built, so it is not kept here.
    await repository.publish_media(PublishedMedia(mxc, event_id))
    server_name, _, media_id = mxc.removeprefix("mxc://").partition("/")
    return {
        "$type": EXTERNAL_EMBED_TYPE,
        "external": {
            "uri": f"{config.bridge.public_base_url}/media/{quote(server_name, safe='')}/{quote(media_id, safe='')}",
            "description": f"{mime_type} · {format_size(len(data))}",
        },
    }


async def publish_event_as_posts(
    request: Request,
    account: Account,
    event: dict,
    *,
    reply_to: PostRef | None = None,
    quote: PostRef | None = None,
    offset: int = 0,
) -> list[dict[str, str]] | None:
    """Publish a Matrix message as a post by ``account`` -- or a thread of
    posts, if its text is longer than one -- and return a reference to
    each. ``None`` if nothing was published, in which case the sender has
    been told why (unless there was simply nothing to say).

    ``reply_to`` makes it a reply to that post. ``quote`` makes it a
    quote-post of that post. ``offset`` skips that many leading characters
    of the text (a command word).
    """
    repository = request.app.state.repository
    room_id = event["room_id"]
    event_id = event["event_id"]
    content = event.get("content") or {}
    parts = post_parts(event, offset=offset)
    if parts is None:
        if content.get("msgtype") != "m.text":  # an empty text message is just ignored
            await notice(request, room_id, "That kind of message can't be published to Bluesky.")
        return None
    if len(parts) > MAX_THREAD_POSTS:
        await notice(
            request,
            room_id,
            f"That would take a thread of {len(parts)} posts; the limit is {MAX_THREAD_POSTS}, so it wasn't published.",
        )
        return None
    embed = None
    if quote is not None:
        embed = {"$type": "app.bsky.embed.record", "record": quote.strong_ref()}
    elif content.get("msgtype") in MEDIA_MSGTYPES:
        embed = await _build_embed(request, account, event)
        if embed is None:
            return None

    # One post, or a thread: each further piece replies to the one before.
    pds = request.app.state.pds
    writes: list[Write] = []
    refs: list[dict[str, str]] = []
    root_ref = reply_to.root_ref() if reply_to is not None else None
    parent_ref = reply_to.strong_ref() if reply_to is not None else None
    for index in range(len(parts)):
        reply = {"root": root_ref, "parent": parent_ref} if parent_ref is not None else None
        part_embed = embed if index == 0 else None
        record = post_record_v1(event, part=index, embed=part_embed, reply=reply, offset=offset)
        if record is None:
            return None
        rkey = pds.next_record_key()
        writes.append(
            Write(
                "create",
                POST_COLLECTION,
                rkey,
                record,
                source=post_source(room_id, event_id, part=index, embed=part_embed, reply=reply, offset=offset),
                matrix_event_id=event_id,
            )
        )
        parent_ref = {
            "uri": f"at://{account.did}/{POST_COLLECTION}/{rkey}",
            "cid": str(CID.for_cbor(cbor.encode(record))),
        }
        root_ref = root_ref or parent_ref
        refs.append(parent_ref)
    try:
        await pds.apply_writes(account.did, writes)
    except PdsError as exc:
        logger.warning("Publishing %s for %s failed: %s", event_id, account.did, exc.message)
        await repository.forget_media_for_event(event_id)
        await notice(request, room_id, "Couldn't publish that to Bluesky.")
        return None
    logger.info("Published %s as %d post(s) for %s", event_id, len(writes), account.did)
    return refs


async def maybe_publish_profile_post(request: Request, event: dict) -> bool:
    """Returns True if this was the owner's message in a Profile Room
    (handled, whether or not it could be published)."""
    if event.get("type") != "m.room.message":
        return False
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    repository = request.app.state.repository
    account = await repository.get_account_for_profile_room(room_id)
    if account is None or event.get("sender") != account.matrix_user_id or not event_id:
        return False
    # Belt and braces: the command handler runs first and consumes these,
    # but a message meant for the bot must never go out as a post.
    content = event.get("content") or {}
    if message_addresses_bot(content, request.app.state.config):
        return True
    if "m.relates_to" in content or "m.new_content" in content:
        # An edit must not post its "* new text" fallback as a new post.
        # (A reply to a post is handled before this, in
        # bridge.interactions; one that reaches here answers something
        # that isn't a post.)
        return True
    if await repository.get_records_for_event(event_id):
        return True  # a transaction Synapse delivered twice
    await publish_event_as_posts(request, account, event)
    return True


async def maybe_handle_redaction(request: Request, event: dict) -> bool:
    """Returns True if this redacted an event records were made from (and
    they, and any media published for them, are gone)."""
    if event.get("type") != "m.room.redaction":
        return False
    # Top-level in older room versions, inside content from v11.
    redacted_id = event.get("redacts") or (event.get("content") or {}).get("redacts")
    if not redacted_id:
        return False
    repository = request.app.state.repository
    # A repost can be undone by redacting either the reaction/command that
    # made it or the card it left in the Profile Room.
    card_of = await repository.get_value(f"{CARD_SOURCE_KEY}:{redacted_id}")
    card = await repository.get_value(f"{EVENT_CARD_KEY}:{card_of or redacted_id}")
    if card_of:
        redacted_id = card_of
    elif card:
        card_room, _, card_event = card.partition("|")
        try:
            await request.app.state.synapse.redact_event(
                card_room, card_event, as_user_id=bot_mxid(request.app.state.config)
            )
        except SynapseError:
            logger.info("Could not redact repost card %s", card_event, exc_info=True)
    rows = await repository.get_records_for_event(redacted_id)
    await repository.forget_media_for_event(redacted_id)
    if not rows:
        return False
    did = rows[0].did
    try:
        await request.app.state.pds.apply_writes(did, [Write("delete", row.collection, row.rkey) for row in rows])
    except PdsError as exc:
        # Already gone: a fetch can notice the redaction before this does.
        logger.info("Deleting records of %s after redaction of %s: %s", did, redacted_id, exc.message)
    else:
        logger.info("Deleted %d record(s) of %s: Matrix event %s was redacted", len(rows), did, redacted_id)
    return True


_PROFILE_STATE_TYPES = ("m.room.name", "m.room.topic", "m.room.avatar", PROFILE_BANNER_STATE_TYPE)


async def maybe_handle_profile_room_state(request: Request, event: dict) -> bool:
    """Returns True if this was a name, topic, avatar or banner change in
    a Profile Room."""
    if event.get("type") not in _PROFILE_STATE_TYPES or event.get("state_key") != "":
        return False
    account = await request.app.state.repository.get_account_for_profile_room(event.get("room_id", ""))
    if account is None:
        return False
    try:
        await sync_profile_record(request.app, account)
    except (SynapseError, PdsError):
        logger.warning("Could not update profile record for %s", account.did, exc_info=True)
    return True
