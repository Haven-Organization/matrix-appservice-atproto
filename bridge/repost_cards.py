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
- otherwise, if the card carries an image or video Bluesky can show, a
  post of the owner's with it embedded: "🔁 Reposted X's post", "post"
  linking to the original, and the post's text (``repost_card_record_v2``);
- otherwise a post linking to the original: "🔁 Reposted X's post" with a
  link card titled with X and described with the post's text
  (``repost_card_record_v1``).

A card without ``external_url`` takes it from the reposted event itself,
which the bridges mirror with one, if the bot can read it (a world-readable
Remote User Room). Without any address the post goes out unlinked, if it
has media or text to show.
Redacting the card (the other bridge does, when the repost is undone)
removes what was made from it, like any other published event.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from bridge.commands import bot_mxid
from bridge.follows import now_timestamp
from bridge.interactions import has_record_of
from bridge.matrix_records import (
    POST_COLLECTION,
    external_url,
    repost_card_record_v1,
    repost_card_record_v2,
    repost_card_source,
    repost_relation,
)
from bridge.msc4501 import PER_MESSAGE_PROFILE_FIELD
from bridge.pds import PdsError, Write
from bridge.post_lookup import find_linked_post, locate_post_by_event
from bridge.post_mirroring import REPOST_COLLECTION
from bridge.profile_posts import _build_embed
from bridge.repository import Account
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


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
        if link is not None:
            linked = await find_linked_post(app, link)
            if linked is not None:
                await _repost(request, account, linked[1], event_id)
                return True
        media = None
        if content.get("msgtype") in ("m.image", "m.video"):
            media = await _build_embed(request, account, event, native_only=True)
        if media is None and link is not None:
            # Nothing to show but the address: a link card to it.
            record = repost_card_record_v1(event, link=kept_link)
            source = repost_card_source(room_id, event_id, link=kept_link)
        else:
            record = repost_card_record_v2(event, media=media, link=kept_link)
            source = repost_card_source(room_id, event_id, media=media, link=kept_link, v=2)
        if record is None:
            logger.info("Repost card %s has nothing to show (no address, media or text); not published", event_id)
            return True
        pds = app.state.pds
        await pds.apply_writes(
            account.did,
            [Write("create", POST_COLLECTION, pds.next_record_key(), record, source=source, matrix_event_id=event_id)],
        )
        logger.info("%s shared a repost of %s (card %s)", account.did, link or relation.get("event_id"), event_id)
    except PdsError as exc:
        logger.warning("Repost card %s for %s failed: %s", event_id, account.did, exc.message)
        await repository.forget_media_for_event(event_id)
    return True
