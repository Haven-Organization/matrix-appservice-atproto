"""Reposts made through another bridge sharing the Profile Room.

When the owner reposts a fediverse or Nostr post, the bridge for that
network leaves a repost card in their Profile Room (MSC4501): a message
from its bot on the owner's behalf (``com.beeper.per_message_profile``
naming the owner), with an ``org.matrix.msc4501.social.relates_to`` repost
relation, the reposted post's text in ``org.matrix.msc4501.social.body``,
a preview image or video, and -- ideally -- the original's web address in
``external_url``.

Such a card, from one of ``appservice.other_bot_users`` (or the owner),
becomes:

- a native repost, if what it reposts is a Bluesky post -- one in Matrix,
  or one ``external_url`` links to on the web;
- otherwise a post of the owner's linking to the original: "🔁 Reposted
  X's post" with a link card titled with X, described with the post's
  text and showing the preview image (``repost_card_record_v1``).

A card without ``external_url`` takes it from the reposted event itself,
which the bridges mirror with one, if the bot can read it (a world-readable
Remote User Room); otherwise there is nothing to link to and the card is
left alone.
Redacting the card (the other bridge does, when the repost is undone)
removes what was made from it, like any other published event.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from bridge.atproto.cid import CID
from bridge.commands import bot_mxid
from bridge.follows import now_timestamp
from bridge.interactions import has_record_of
from bridge.matrix_records import (
    POST_COLLECTION,
    media_filename,
    external_url,
    repost_card_record_v1,
    repost_card_source,
    repost_relation,
)
from bridge.media import prepare_image
from bridge.msc4501 import PER_MESSAGE_PROFILE_FIELD
from bridge.pds import PdsError, Write
from bridge.post_lookup import find_linked_post, locate_post_by_event
from bridge.post_mirroring import REPOST_COLLECTION
from bridge.repository import Account, BlobRow
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


async def _thumb(request: Request, account: Account, event: dict) -> dict[str, Any] | None:
    """A blob of the card's preview image, if it has one that can be used."""
    content = event.get("content") or {}
    mxc = content.get("url")
    if content.get("msgtype") != "m.image" or not isinstance(mxc, str) or "file" in content:
        return None
    synapse = request.app.state.synapse
    try:
        download = await synapse.download_media(mxc)
    except SynapseError:
        logger.info("Could not download repost card preview %s", mxc, exc_info=True)
        return None
    image = await prepare_image(download.content) if download is not None else None
    if image is None:
        return None
    if image.reencoded:
        try:
            mxc = await synapse.upload_media(
                image.data, image.mime_type, media_filename(event), as_user_id=bot_mxid(request.app.state.config)
            )
        except SynapseError:
            logger.info("Could not store re-encoded preview for %s", event["event_id"], exc_info=True)
            return None
    cid = CID.for_raw(image.data)
    await request.app.state.repository.add_blob(
        BlobRow(account.did, str(cid), mxc, image.mime_type, len(image.data), event["event_id"])
    )
    return {"$type": "blob", "ref": cid, "mimeType": image.mime_type, "size": len(image.data)}


async def _original_link(request: Request, relation: dict[str, Any]) -> str | None:
    """The reposted event's own ``external_url``, if the bot can read it."""
    room_id, event_id = relation.get("room_id"), relation.get("event_id")
    if not isinstance(room_id, str) or not isinstance(event_id, str) or not room_id or not event_id:
        return None
    try:
        original = await request.app.state.synapse.get_event(
            room_id, event_id, as_user_id=bot_mxid(request.app.state.config)
        )
    except SynapseError:
        return None
    return external_url(original) if original else None


async def _repost(request: Request, account: Account, subject: dict[str, str], event_id: str) -> None:
    if await has_record_of(request, account, REPOST_COLLECTION, subject["uri"]):
        return
    pds = request.app.state.pds
    record = {"$type": REPOST_COLLECTION, "subject": subject, "createdAt": now_timestamp()}
    await pds.apply_writes(
        account.did, [Write("create", REPOST_COLLECTION, pds.next_record_key(), record, matrix_event_id=event_id)]
    )
    logger.info("%s reposted %s (card %s)", account.did, subject["uri"], event_id)


async def maybe_publish_repost_card(request: Request, event: dict) -> bool:
    """Returns True if this was a repost card in a Profile Room made for
    its owner by another bridge (handled, whether or not anything could be
    published from it)."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    relation = repost_relation(content)
    if relation is None:
        return False
    app = request.app
    repository = app.state.repository
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    sender = event.get("sender", "")
    account = await repository.get_account_for_profile_room(room_id)
    if account is None or not event_id:
        return False
    if sender != account.matrix_user_id:
        if sender not in app.state.config.appservice.other_bot_users:
            return False  # this bridge's own cards, or anyone else's message
        if (content.get(PER_MESSAGE_PROFILE_FIELD) or {}).get("id") != account.matrix_user_id:
            return True  # a card for someone else's repost
    if await repository.get_records_for_event(event_id):
        return True  # a transaction Synapse delivered twice
    try:
        target = await locate_post_by_event(app, str(relation.get("room_id") or ""), str(relation.get("event_id") or ""))
        if target is not None:
            await _repost(request, account, target.strong_ref(), event_id)
            return True
        link = external_url(event)
        kept_link = None
        if link is None:
            kept_link = link = await _original_link(request, relation)
        if link is None:
            logger.info("Repost card %s has no external_url to link to; not published", event_id)
            return True
        linked = await find_linked_post(app, link)
        if linked is not None:
            await _repost(request, account, linked[1], event_id)
            return True
        thumb = await _thumb(request, account, event)
        record = repost_card_record_v1(event, thumb=thumb, link=kept_link)
        if record is None:
            return True
        pds = app.state.pds
        await pds.apply_writes(
            account.did,
            [
                Write(
                    "create",
                    POST_COLLECTION,
                    pds.next_record_key(),
                    record,
                    source=repost_card_source(room_id, event_id, thumb=thumb, link=kept_link),
                    matrix_event_id=event_id,
                )
            ],
        )
        logger.info("%s shared a repost of %s (card %s)", account.did, link, event_id)
    except PdsError as exc:
        logger.warning("Repost card %s for %s failed: %s", event_id, account.did, exc.message)
        await repository.forget_media_for_event(event_id)
    return True
