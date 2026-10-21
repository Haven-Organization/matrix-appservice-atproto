"""Acting on posts from Matrix: liking, reposting, quoting and replying.

Same triggers as matrix-appservice-activitypub:

- Reacting to a post with a thumbs up -- in any skin tone -- likes it.
  (Bluesky has no other reactions, so other emoji stay Matrix-only.)
- Reacting with 🔁 reposts it. So does the ``repost`` command sent as a
  reply; with text after it, that makes a quote-post instead.
- Replying to a post (a rich reply, or a message in its thread) publishes
  a reply to it.
- Removing the reaction, or redacting the message, undoes each.

"A post" is any Matrix event that stands for one: a mirrored remote post
in any room, or a local user's own post. The action is taken as whoever
did it in Matrix, so they need an identity of their own.

A repost or quote also leaves a card in the reposter's own Profile Room
("🔁 reposted X's post"), since that -- not the room the post happened to
be in -- is their timeline. The bot sends it, with an MSC4144 per-message
profile so it shows as the user's own action.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from fastapi import Request

from bridge.commands import bot_mxid, is_bot_user, message_addresses_bot, message_for_other_bot, notice
from bridge.follows import now_timestamp
from bridge.matrix_records import post_text
from bridge.msc4501 import (
    PER_MESSAGE_PROFILE_FIELD,
    SOCIAL_BODY_FIELD,
    SOCIAL_FORMATTED_BODY_FIELD,
    SOCIAL_REL_TYPE_REPOST,
    SOCIAL_RELATES_TO_FIELD,
    social_relates_to,
)
from bridge.pds import PdsError, Write
from bridge.post_lookup import PostRef, locate_post_by_event
from bridge.post_mirroring import REPOST_COLLECTION, at_uri_to_url, matrix_event_link
from bridge.previews import build_preview_media_content, fetch_post_preview, quote_block, user_display
from bridge.profile_posts import CARD_SOURCE_KEY, EVENT_CARD_KEY, publish_event_as_posts
from bridge.repository import Account
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

LIKE_COLLECTION = "app.bsky.feed.like"

_THUMBS_UP = "\U0001F44D"
_VARIATION_SELECTOR_16 = "️"
_SKIN_TONE_MODIFIERS = "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"
_REPOST_EMOJI = "\U0001F501"


def _bare_emoji(key: str) -> str:
    """``key`` without the emoji-presentation selector some clients append
    and without any skin-tone modifier, so every thumbs up is the same."""
    stripped = key.replace(_VARIATION_SELECTOR_16, "")
    for modifier in _SKIN_TONE_MODIFIERS:
        stripped = stripped.replace(modifier, "")
    return stripped


async def has_record_of(request: Request, account: Account, collection: str, subject_uri: str) -> bool:
    """Whether ``account`` already has a like/repost of ``subject_uri``.
    Both are stored inline, so their subject can be read back."""
    for row in await request.app.state.repository.list_records(account.did):
        if row.collection == collection:
            if ((json.loads(row.source).get("record") or {}).get("subject") or {}).get("uri") == subject_uri:
                return True
    return False


async def per_message_profile(request: Request, account: Account) -> dict[str, Any] | None:
    """MSC4144 profile for a message the bot sends on ``account``'s
    behalf, read from their Profile Room's live name and avatar so it
    always matches their real profile. ``None`` if turned off."""
    config = request.app.state.config
    if not config.bridge.msc4144_per_message_profiles or not account.profile_room_id:
        return None
    synapse = request.app.state.synapse
    bot = bot_mxid(config)
    profile: dict[str, Any] = {"id": account.matrix_user_id}
    try:
        name = ((await synapse.get_room_state(account.profile_room_id, "m.room.name", as_user_id=bot)) or {}).get("name")
        avatar = ((await synapse.get_room_state(account.profile_room_id, "m.room.avatar", as_user_id=bot)) or {}).get("url")
    except SynapseError:
        name = avatar = None
    if name:
        profile["displayname"] = name
    if avatar:
        profile["avatar_url"] = avatar
    return profile


async def _post_card(
    request: Request, account: Account, target: PostRef, *, source_event_id: str, caption: str | None
) -> None:
    """Leave the "🔁 reposted X's post" card in ``account``'s Profile
    Room, and tie it to ``source_event_id`` so that redacting either
    undoes the repost. Formatted like matrix-appservice-activitypub's:
    "post" is a link to the post in Matrix, a preview of it follows, and
    if it is an image or video the card is that image or video at
    thumbnail size with the text as its caption. Like the sibling bridges'
    cards it carries the owner's per-message profile and the post's web
    address (``external_url``)."""
    app = request.app
    config = app.state.config
    synapse = app.state.synapse
    repository = app.state.repository
    if not account.profile_room_id:
        return
    bot = bot_mxid(config)
    preview_text, full_content, preview_image, preview_video = await fetch_post_preview(app, target.room_id, target.event_id)
    link = matrix_event_link(app, target.room_id, target.event_id)
    author_name, author_html = await user_display(app, target.sender)
    _, reposter_html = await user_display(app, account.matrix_user_id)
    quoted_plain, quoted_html = quote_block(preview_text)
    post_html = f'<a href="{html.escape(link, quote=True)}">post</a>'
    caption_html = f"<p>{html.escape(caption)}</p>" if caption else ""
    formatted = f"{caption_html}<p>🔁 {reposter_html} reposted {author_html}'s {post_html}</p>{quoted_html}"
    plain = (f"{caption}\n\n" if caption else "") + f"🔁 reposted {author_name}'s post:"
    if quoted_plain:
        plain += f"\n{quoted_plain}"
    plain += f"\n{link}"

    # A pure repost's card stands for the post itself: the relation says
    # so (content_inline) and, per MSC4501, the body is then nothing but
    # the link. A quote has commentary of its own, so it keeps its full
    # body and carries the original's content in the relation instead.
    relation = None
    if config.bridge.set_msc4501_relates_to and full_content:
        relation = social_relates_to(
            SOCIAL_REL_TYPE_REPOST,
            event_id=target.event_id,
            room_id=target.room_id,
            sender=target.sender,
            via=[config.synapse.server_name],
            displayname=author_name,
            content_inline=caption is None,
            content=None if caption is None else full_content,
        )
    content = build_preview_media_content(
        plain_body=link if relation and caption is None else plain,
        formatted_caption=formatted,
        preview_image=preview_image,
        preview_video=preview_video,
    )
    # The reposted post on the web, as on every post the bridge mirrors:
    # what a sibling bridge sharing the room links to when it publishes
    # this repost on its own network.
    content["external_url"] = at_uri_to_url(target.uri) or link
    if relation:
        content[SOCIAL_RELATES_TO_FIELD] = relation
        # What an MSC4501 client shows in place of the body: for a quote,
        # the commentary alone; for a pure repost, the reposted post's text.
        social_text = caption if caption is not None else preview_text
        if social_text:
            content[SOCIAL_BODY_FIELD] = social_text
            content[SOCIAL_FORMATTED_BODY_FIELD] = html.escape(social_text)
    profile = await per_message_profile(request, account)
    if profile is not None:
        content[PER_MESSAGE_PROFILE_FIELD] = profile
    try:
        card_event_id = await synapse.send_message_event(account.profile_room_id, content, as_user_id=bot)
    except SynapseError:
        logger.warning("Failed to post repost card in %s", account.profile_room_id, exc_info=True)
        return
    await repository.set_value(f"{EVENT_CARD_KEY}:{source_event_id}", f"{account.profile_room_id}|{card_event_id}")
    await repository.set_value(f"{CARD_SOURCE_KEY}:{card_event_id}", source_event_id)


async def send_repost(request: Request, account: Account, target: PostRef, *, source_event_id: str) -> bool:
    """Repost ``target`` as ``account``. ``source_event_id`` is the Matrix
    event that asked for it (a 🔁 reaction, or a ``repost`` command).
    Returns False if they had already reposted it."""
    pds = request.app.state.pds
    if await has_record_of(request, account, REPOST_COLLECTION, target.uri):
        return False
    record = {"$type": REPOST_COLLECTION, "subject": target.strong_ref(), "createdAt": now_timestamp()}
    await pds.apply_writes(
        account.did,
        [Write("create", REPOST_COLLECTION, pds.next_record_key(), record, matrix_event_id=source_event_id)],
    )
    await _post_card(request, account, target, source_event_id=source_event_id, caption=None)
    logger.info("%s reposted %s", account.did, target.uri)
    return True


async def send_quote(request: Request, account: Account, target: PostRef, event: dict, *, offset: int) -> bool:
    """Publish ``event``'s text (past ``offset``) as a quote-post of
    ``target`` by ``account``."""
    refs = await publish_event_as_posts(request, account, event, quote=target, offset=offset)
    if refs is None:
        return False
    await _post_card(
        request, account, target, source_event_id=event["event_id"], caption=post_text(event, offset=offset) or ""
    )
    logger.info("%s quoted %s", account.did, target.uri)
    return True


async def maybe_handle_reaction(request: Request, event: dict) -> bool:
    """Returns True if this was a thumbs-up or 🔁 reaction to a post by a
    local user (acted on, or explained)."""
    if event.get("type") != "m.reaction":
        return False
    relates_to = (event.get("content") or {}).get("m.relates_to") or {}
    key = _bare_emoji(str(relates_to.get("key") or ""))
    if relates_to.get("rel_type") != "m.annotation" or key not in (_THUMBS_UP, _REPOST_EMOJI):
        return False
    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    if not sender or not event_id or is_bot_user(config, sender):
        return False
    target = await locate_post_by_event(app, room_id, str(relates_to.get("event_id") or ""))
    if target is None:
        return False  # a reaction to something that isn't a post
    repository = app.state.repository
    account = await repository.get_account_for_user(sender)
    if account is None:
        if sender.endswith(f":{config.synapse.server_name}"):
            p = config.appservice.command_prefix
            await notice(request, room_id, f'{sender}: you need a Bluesky identity to do that -- run "{p}create profile".')
        return True
    if await repository.get_records_for_event(event_id):
        return True  # a transaction Synapse delivered twice
    pds = app.state.pds
    try:
        if key == _REPOST_EMOJI:
            await send_repost(request, account, target, source_event_id=event_id)
        elif not await has_record_of(request, account, LIKE_COLLECTION, target.uri):
            record = {"$type": LIKE_COLLECTION, "subject": target.strong_ref(), "createdAt": now_timestamp()}
            await pds.apply_writes(
                account.did,
                [Write("create", LIKE_COLLECTION, pds.next_record_key(), record, matrix_event_id=event_id)],
            )
            logger.info("%s liked %s", account.did, target.uri)
    except PdsError as exc:
        logger.warning("Reaction %s by %s failed: %s", event_id, account.did, exc.message)
    return True


def _reply_target(content: dict) -> str | None:
    """The event a message answers: the one it replies to, or -- a thread
    message that replies to nothing in particular -- the thread's root."""
    relates_to = content.get("m.relates_to") or {}
    in_reply_to = (relates_to.get("m.in_reply_to") or {}).get("event_id")
    if relates_to.get("rel_type") == "m.thread":
        if in_reply_to and not relates_to.get("is_falling_back"):
            return in_reply_to
        return in_reply_to or relates_to.get("event_id")
    return in_reply_to if isinstance(in_reply_to, str) else None


async def maybe_publish_reply(request: Request, event: dict) -> bool:
    """Returns True if this was a local user's reply (a rich reply or a
    thread message) to a post, published as a reply from their account."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    target_event = _reply_target(content)
    if not target_event or not sender or not event_id or is_bot_user(config, sender):
        return False
    if "m.new_content" in content or message_addresses_bot(content, config):
        return False
    if message_for_other_bot(content, config):
        return True  # e.g. another bridge's command given as a reply to a post
    target = await locate_post_by_event(app, room_id, target_event, last_part=True)
    if target is None:
        return False
    repository = app.state.repository
    account = await repository.get_account_for_user(sender)
    if account is None:
        if sender.endswith(f":{config.synapse.server_name}"):
            p = config.appservice.command_prefix
            await notice(request, room_id, f'{sender}: you need a Bluesky identity to reply -- run "{p}create profile".')
        return True
    if await repository.get_records_for_event(event_id):
        return True  # a transaction Synapse delivered twice
    await publish_event_as_posts(request, account, event, reply_to=target)
    return True

