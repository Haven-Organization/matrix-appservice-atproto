"""Matrix Application Service transaction receiver.

This is the homeserver -> bridge half of the AS API: Synapse PUTs batches
of Matrix room events here whenever something happens in a room the bot is
a member of, authenticating itself with the shared ``hs_token`` from the
registration file.

It authenticates, skips events it has already handled (Synapse resends a
transaction on timeout/error; remembered by event ID, persisted, so safe
across a restart of either side), swaps an encrypted file already decrypted
once for its plaintext copy, then for each event tries, in order: accepting
an invite extended to the bot; a confirmation of an encrypted file's prompt,
or another bridge's decrypted copy of one (see ``bridge.encrypted_media``); a ghost
being invited to a room to start a chat; a local user joining a room the
bridge manages for them (which files it in their space) or leaving a
Remote User Room (which unfollows); a redaction (of a chat message, or of
an event records were made from); a Profile Room's name, topic, avatar or
banner changing; an edit of an already-published Profile Room post (see
``bridge.edit_bridge``); a "confirm" reply to a Danger Zone warning (see
``bridge.danger``); a command addressed to the bot (see
``bridge.commands``); a message in a chat room (see ``bridge.chat``); a
like or repost reaction, or a reply, to a post (see
``bridge.interactions``); a repost card another bridge left in a Profile
Room (see ``bridge.repost_cards``); and publishing the owner's message in a Profile
Room (see ``bridge.profile_posts``). The first handler to claim an event ends its
processing.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from bridge.chat import (
    maybe_handle_chat_redaction,
    maybe_send_chat_message,
    maybe_send_chat_reaction,
    maybe_start_chat_from_invite,
)
from bridge.repost_cards import maybe_publish_repost_card
from bridge.commands import bot_mxid, is_bot_user, maybe_handle_command, notice
from bridge.danger import maybe_handle_confirmation
from bridge.edit_bridge import maybe_publish_edit
from bridge.encrypted_media import done_handling as encrypted_media_done
from bridge.encrypted_media import handling as encrypted_media_handling
from bridge.encrypted_media import maybe_handle_encrypted_media
from bridge.encrypted_media import resolve as resolve_encrypted_media
from bridge.interactions import maybe_handle_reaction, maybe_publish_reply
from bridge.membership import maybe_handle_join, maybe_handle_knock, maybe_handle_leave
from bridge.profile_posts import maybe_handle_profile_room_state, maybe_handle_redaction, maybe_publish_profile_post
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

router = APIRouter()

_EMPTY_JSON_RESPONSE_KWARGS = {"status_code": 200, "content": "{}", "media_type": "application/json"}


def _check_hs_token(request: Request, authorization: str | None, access_token: str | None) -> None:
    """Verify the request is from our homeserver. Synapse presents hs_token
    as a Bearer header, or (older versions) an ``access_token`` query parameter."""
    expected = request.app.state.config.appservice.hs_token
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :]
    elif access_token:
        presented = access_token
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid or missing hs_token")


async def maybe_accept_invite(request: Request, event: dict) -> bool:
    """Returns True if this was an invite to the bot (joined, if possible).

    Without this the bot is invited into rooms but never joins them -- and
    Synapse only pushes events for rooms where one of the bridge's users is
    actually a member.
    """
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    config = request.app.state.config
    bot = bot_mxid(config)
    if content.get("membership") != "invite" or event.get("state_key") != bot:
        return False
    room_id = event.get("room_id", "")
    inviter = event.get("sender", "")
    synapse = request.app.state.synapse
    try:
        await synapse.join_room(room_id, as_user_id=bot)
    except SynapseError:
        logger.warning("Could not join %s on invite from %s", room_id, inviter, exc_info=True)
        return True

    # A real local person, not a ghost inviting the bot into a chat room.
    is_local = inviter.split(":", 1)[-1] == config.synapse.server_name and not is_bot_user(config, inviter)
    try:
        encrypted = await synapse.get_room_state(room_id, "m.room.encryption", as_user_id=bot) is not None
    except SynapseError:
        encrypted = False
    if encrypted:
        # The bridge has no end-to-end encryption support, and clients
        # commonly create direct chats encrypted by default.
        await notice(
            request,
            room_id,
            "This room is encrypted, so I can't read anything said in it. "
            "Please invite me to a new room with encryption turned off.",
        )
    elif content.get("is_direct") and is_local:
        prefix = config.appservice.command_prefix
        await notice(
            request, room_id, f'Hi! Say "{prefix}help" to see what I can do, or "{prefix}create profile" to get started.'
        )
    return True


async def handle_event(request: Request, event: dict[str, Any]) -> None:
    """Handle one event: an encrypted file already decrypted once is swapped
    for its plaintext copy first (see ``bridge.encrypted_media``)."""
    event = await resolve_encrypted_media(request, event)
    token = encrypted_media_handling(event)
    try:
        await _handle_event(request, event)
    finally:
        encrypted_media_done(token)


async def _handle_event(request: Request, event: dict[str, Any]) -> None:
    """Give ``event`` to the first handler that claims it."""
    if await maybe_accept_invite(request, event):
        return
    # Before anything that could publish or answer it: a confirmation of an
    # encrypted file's prompt, or another bridge's decrypted copy of one.
    if await maybe_handle_encrypted_media(request, event):
        return
    if await maybe_start_chat_from_invite(request, event):
        return
    if await maybe_handle_knock(request, event):
        return
    if await maybe_handle_join(request, event):
        return
    if await maybe_handle_leave(request, event):
        return
    if await maybe_handle_chat_redaction(request, event):
        return
    if await maybe_handle_redaction(request, event):
        return
    if await maybe_publish_edit(request, event):
        return
    if await maybe_handle_profile_room_state(request, event):
        return
    # Before commands and posting: a bare "confirm" is recognized
    # by what it replies to, not by a prefix or a tag.
    if await maybe_handle_confirmation(request, event):
        return
    if await maybe_handle_command(request, event):
        return
    if await maybe_send_chat_message(request, event):
        return
    if await maybe_send_chat_reaction(request, event):
        return
    if await maybe_handle_reaction(request, event):
        return
    if await maybe_publish_repost_card(request, event):
        return
    if await maybe_publish_reply(request, event):
        return
    await maybe_publish_profile_post(request, event)


async def _handle_transaction(
    request: Request, txn_id: str, authorization: str | None, access_token: str | None
) -> Response:
    _check_hs_token(request, authorization, access_token)
    repository = request.app.state.repository

    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed transaction body: {exc}") from exc

    for event in body.get("events", []):
        # Deduplicated by event, not by txnId: Synapse on SQLite numbers its
        # transactions from 1 again after a restart, so a repeated txnId can
        # carry new events. A resent transaction also gets the events that
        # weren't handled before the bridge stopped.
        event_id = event.get("event_id")
        if event_id and await repository.has_processed_event(event_id):
            logger.debug("Ignoring already-processed event %s in transaction %s", event_id, txn_id)
            continue
        try:
            await handle_event(request, event)
        except Exception:
            logger.exception(
                "Error handling event %s (%s) in %s", event_id, event.get("type"), event.get("room_id")
            )
        if event_id:
            await repository.mark_event_processed(event_id)

    return Response(**_EMPTY_JSON_RESPONSE_KWARGS)


@router.put("/_matrix/app/v1/transactions/{txn_id}")
async def put_transaction_v1(
    request: Request,
    txn_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    return await _handle_transaction(request, txn_id, authorization, access_token)


@router.put("/transactions/{txn_id}")
async def put_transaction_legacy(
    request: Request,
    txn_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    """Unprefixed pre-v1 path, kept for compatibility with older homeserver configs."""
    return await _handle_transaction(request, txn_id, authorization, access_token)
