"""Showing Bluesky likes and reposts as Matrix reactions.

When someone on Bluesky likes or reposts a post that is in Matrix -- a
local user's own post, or a mirrored one -- their ghost reacts to the
Matrix message with 👍 or 🔁, the same emoji that mean "like" and "repost"
going the other way (see ``bridge.interactions``).

Two sources feed this, because no single one sees everything:

- A local account's notifications (``bridge.notification_poller``) report
  every like and repost of ITS posts, by anyone -- but never their removal.
- The live feed (``bridge.jetstream``) reports likes by accounts FOLLOWED
  here, of any post, and their removal.

Each reaction is remembered by the address of the like or repost record
behind it, so the two sources can't double up and a removal can find what
to redact.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.atproto.identity import resolve_did
from bridge.post_lookup import locate_post_by_uri
from bridge.post_mirroring import _bot, _ensure_ghost_in_room
from bridge.remote_accounts import ensure_ghost
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

LIKE_EMOJI = "\U0001F44D"
REPOST_EMOJI = "\U0001F501"


def _key(record_uri: str) -> str:
    return f"reaction_for:{record_uri}"


async def mirror_reaction(app: FastAPI, *, actor_did: str, record_uri: str, subject_uri: str, emoji: str) -> bool:
    """React, as ``actor_did``'s ghost, to the Matrix message standing for
    ``subject_uri``. ``record_uri`` is the like or repost record this is
    for. Returns False if there is nothing to react to, it was already
    done, or the actor is a local user (whose own Matrix reaction, or
    absence of one, already says it)."""
    repository = app.state.repository
    if not record_uri or await repository.get_value(_key(record_uri)) is not None:
        return False
    if await repository.get_account(actor_did) is not None:
        return False
    target = await locate_post_by_uri(app, subject_uri)
    if target is None:
        return False
    identity = await resolve_did(app.state.http_client, actor_did, plc_directory=app.state.config.atproto.plc_directory)
    if identity is None:
        return False
    try:
        ghost, *_ = await ensure_ghost(app, identity)
        if not await _ensure_ghost_in_room(app, ghost, target.room_id):
            return False
        event_id = await app.state.synapse.send_reaction(target.room_id, target.event_id, emoji, as_user_id=ghost)
    except SynapseError:
        logger.info("Could not mirror reaction for %s", record_uri, exc_info=True)
        return False
    await repository.set_value(_key(record_uri), f"{target.room_id}|{event_id}")
    return True


async def unmirror_reaction(app: FastAPI, record_uri: str) -> None:
    """Redact the reaction made for a like or repost that was undone."""
    repository = app.state.repository
    stored = await repository.get_value(_key(record_uri))
    if stored is None:
        return
    room_id, _, event_id = stored.partition("|")
    try:
        await app.state.synapse.redact_event(room_id, event_id, as_user_id=_bot(app))
    except SynapseError:
        logger.info("Could not redact mirrored reaction %s", event_id, exc_info=True)
    await repository.delete_value(_key(record_uri))
