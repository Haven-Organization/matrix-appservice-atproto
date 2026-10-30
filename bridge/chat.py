"""Bluesky chat: direct messages, as Matrix DM rooms.

Bluesky chat is not part of a repository and never touches the PDS: it
lives on Bluesky's own chat service. The bridge talks to that service on
each local account's behalf with a short-lived token signed by the
account's key (``bridge.atproto.service_auth``), exactly as it does for
notifications.

Each conversation between a local account and a remote one is one Matrix
room, created by the remote account's ghost as a direct chat with the
local user and named "<handle> (Chat)". A message the local user sends
there goes out as a chat message; ``chat_loop`` asks the service for each
account's new messages and posts them as the ghost.

Bluesky has one kind of direct message, so ``dm`` and ``chat`` are the
same command.

Chat messages are text only. A file sent in a chat room goes out as a link
to it, served by the bridge. Replies and emoji reactions carry over in
both directions.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request

from bridge.atproto.identity import RemoteIdentity, post_url, resolve_did
from bridge.atproto.keys import SigningKey
from bridge.atproto.service_auth import service_auth_token
from bridge.matrix_records import MEDIA_MSGTYPES, media_filename, message_body, rich_parts
from bridge.post_mirroring import render_text
from bridge.ghost_ids import did_for_ghost, ghost_user_id
from bridge.remote_accounts import ensure_ghost
from bridge.repository import Account, ChatRoom, PublishedMedia
from bridge.room_widget import add_bridge_widget
from bridge.spaces import add_room_to_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

DECLARATION_COLLECTION = "chat.bsky.actor.declaration"
# The chat service's limit is 1000 graphemes; code points never exceed that.
_MAX_MESSAGE_LENGTH = 1000
_MAX_MESSAGE_PARTS = 10


class ChatError(Exception):
    """The chat service refused or couldn't be reached. The message is
    safe to show the user."""


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def chat_call(
    app: FastAPI, account: Account, method: str, *, params: dict | None = None, body: dict | None = None
) -> dict[str, Any]:
    """Call ``method`` on the chat service as ``account``."""
    config = app.state.config
    token = service_auth_token(
        SigningKey.from_hex(account.signing_key_hex),
        issuer=account.did,
        audience=config.atproto.chat_did,
        method=method,
    )
    url = f"{config.atproto.chat_url}/xrpc/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if body is not None:
            response = await app.state.http_client.post(url, json=body, headers=headers)
        else:
            response = await app.state.http_client.get(url, params=params, headers=headers)
        data = response.json() if response.content else {}
    except Exception as exc:  # network trouble or a malformed body
        raise ChatError("Bluesky's chat service couldn't be reached.") from exc
    if response.status_code != 200:
        raise ChatError(str(data.get("message") or f"Chat service error (HTTP {response.status_code})."))
    return data if isinstance(data, dict) else {}


async def ensure_declaration(app: FastAPI, account: Account) -> None:
    """Publish who may start a chat with the account. Without this record
    the chat service treats the account as not accepting chats at all."""
    if app.state.config.atproto.chat_poll_seconds <= 0:
        return  # chat is turned off: nothing would ever read a chat
    record = {"$type": DECLARATION_COLLECTION, "allowIncoming": app.state.config.atproto.chat_allow_incoming}
    await app.state.pds.put_record(account.did, DECLARATION_COLLECTION, "self", record)


async def ensure_chat_room(
    app: FastAPI,
    account: Account,
    identity: RemoteIdentity,
    convo_id: str,
    *,
    predecessor: dict[str, str] | None = None,
    replace: bool = False,
) -> str:
    """The Matrix room for ``account``'s chat with ``identity``, created
    if there isn't one (or ``replace`` asks for a fresh one) -- in which
    case the user is invited as part of creating it; an existing room
    they've left gets a fresh invite. Raises ``SynapseError`` if a room
    can't be made."""
    repository = app.state.repository
    synapse = app.state.synapse
    existing = await repository.get_chat_room(account.did, identity.did)
    if existing is not None and not replace:
        if existing.convo_id != convo_id:
            await repository.save_chat_room(ChatRoom(existing.room_id, account.did, identity.did, convo_id))
        await _reinvite(app, existing.room_id, account.matrix_user_id, ghost_user_id(app, identity.did))
        return existing.room_id
    ghost, _label, _description, avatar_mxc, _profile = await ensure_ghost(app, identity)
    bot = _bot(app)
    # Created by the ghost, with the user invited as part of creating it.
    # With bridge.chat_rooms_as_dms that invite is a direct-message one,
    # which is what makes clients file the room as a DM with that person
    # (nobody else may invite the user after: a later invite replaces
    # this one and loses the flag). The bot is invited so it can speak in
    # the room (errors, command replies).
    room_id = await synapse.create_room(
        as_user_id=ghost,
        name=f"{identity.handle} (Chat)",
        invite=[account.matrix_user_id, bot],
        is_direct=app.state.config.bridge.chat_rooms_as_dms,
        preset="trusted_private_chat",
        avatar_mxc=avatar_mxc,
        predecessor=predecessor,
    )
    await synapse.join_room(room_id, as_user_id=bot)
    await repository.save_chat_room(ChatRoom(room_id, account.did, identity.did, convo_id))
    await add_bridge_widget(app, room_id)
    await add_room_to_space(app, account.matrix_user_id, room_id)
    logger.info("Created chat room %s for %s with %s", room_id, account.matrix_user_id, identity.handle)
    return room_id


async def _reinvite(app: FastAPI, room_id: str, user_id: str, ghost: str) -> None:
    """Invite the chat's owner back into its room if they've left it. An
    invite still pending is left alone: inviting again would replace it,
    and with it the direct-message flag it may carry."""
    synapse = app.state.synapse
    bot = _bot(app)
    try:
        member = await synapse.get_room_state(room_id, "m.room.member", user_id, as_user_id=bot)
        if (member or {}).get("membership") in ("join", "invite"):
            return
        direct = app.state.config.bridge.chat_rooms_as_dms
        try:
            await synapse.invite_user(room_id, user_id, as_user_id=ghost, is_direct=direct)
        except SynapseError:
            # The ghost can't (it left, say); the bot can, though not as a DM.
            await synapse.invite_user(room_id, user_id, as_user_id=bot)
    except SynapseError as exc:
        logger.info("Invite of %s to %s: %s", user_id, room_id, exc)


async def start_chat(app: FastAPI, account: Account, identity: RemoteIdentity) -> str:
    """Open (or reuse) the chat with ``identity``. Returns the room ID.
    Raises ``ChatError`` if the chat service won't allow the conversation
    (the other account doesn't accept chats from this one, say)."""
    await ensure_declaration(app, account)
    data = await chat_call(app, account, "chat.bsky.convo.getConvoForMembers", params={"members": [identity.did]})
    convo_id = str((data.get("convo") or {}).get("id", ""))
    if not convo_id:
        raise ChatError("The chat service didn't return a conversation.")
    return await ensure_chat_room(app, account, identity, convo_id)


async def maybe_start_chat_from_invite(request: Request, event: dict) -> bool:
    """Returns True if this was a local user inviting a ghost into a room
    of their own -- the other way to start a chat, besides the command.
    The ghost joins and the room becomes that chat's room, unless the
    pair already has one or the chat can't be opened."""
    if event.get("type") != "m.room.member" or (event.get("content") or {}).get("membership") != "invite":
        return False
    app = request.app
    ghost = event.get("state_key", "")
    did = did_for_ghost(app, ghost)
    if did is None:
        return False
    repository = app.state.repository
    room_id = event.get("room_id", "")
    inviter = event.get("sender", "")
    account = await repository.get_account_for_user(inviter)
    if account is None or await repository.get_remote_account_for_room(room_id) is not None:
        return True  # nothing to chat as, or just a ghost being put in its own room
    if await repository.get_chat_room_for_room(room_id) is not None or await repository.get_chat_room(account.did, did):
        return True
    config = app.state.config
    synapse = app.state.synapse
    identity = await resolve_did(app.state.http_client, did, plc_directory=config.atproto.plc_directory)
    if identity is None:
        return True
    try:
        await ensure_declaration(app, account)
        data = await chat_call(app, account, "chat.bsky.convo.getConvoForMembers", params={"members": [did]})
        convo_id = str((data.get("convo") or {}).get("id", ""))
        if not convo_id:
            return True
        await ensure_ghost(app, identity)
        await synapse.join_room(room_id, as_user_id=ghost)
        await repository.save_chat_room(ChatRoom(room_id, account.did, did, convo_id))
        await add_room_to_space(app, inviter, room_id)
    except (ChatError, SynapseError) as exc:
        logger.info("Could not start chat from invite in %s: %s", room_id, exc)
    return True


async def _notice(app: FastAPI, room_id: str, message: str) -> None:
    try:
        await app.state.synapse.send_message_event(room_id, {"msgtype": "m.notice", "body": message}, as_user_id=_bot(app))
    except SynapseError:
        logger.info("Could not send notice to %s", room_id, exc_info=True)


async def maybe_send_chat_message(request: Request, event: dict) -> bool:
    """Returns True if this was the local user's message in one of their
    chat rooms (sent on, or explained why not)."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    repository = app.state.repository
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    chat = await repository.get_chat_room_for_room(room_id)
    if chat is None:
        return False
    account = await repository.get_account(chat.local_did)
    if account is None or event.get("sender") != account.matrix_user_id or not event_id:
        return True  # only the chat's owner speaks for it
    content = event.get("content") or {}
    if "m.new_content" in content:
        return True  # an edit: chat messages can't be edited
    if await repository.get_chat_message_for_event(event_id) is not None:
        return True  # a transaction Synapse delivered twice

    msgtype = content.get("msgtype")
    body = message_body(content)
    # A reply to a message in this chat is sent as a reply to it.
    target_event = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
    reply_to = await repository.get_chat_message_for_event(target_event) if target_event else None
    if msgtype in MEDIA_MSGTYPES:
        mxc = content.get("url")
        if not isinstance(mxc, str) or "file" in content:
            from bridge.encrypted_media import encrypted_file, hold

            if encrypted_file(content) is not None:
                hold(app)  # sent once a decrypted copy is confirmed (bridge.encrypted_media)
                return True
            await _notice(app, room_id, "That attachment is encrypted, so I can't send it.")
            return True
        # Chat has no attachments: send a link to the file instead.
        await repository.publish_media(PublishedMedia(mxc, event_id))
        server_name, _, media_id = mxc.removeprefix("mxc://").partition("/")
        link = f"{app.state.config.bridge.public_base_url}/media/{quote(server_name, safe='')}/{quote(media_id, safe='')}"
        caption = body.strip() if content.get("filename") not in (None, content.get("body")) else ""
        text = f"{caption}\n{link}".strip() if caption else f"{media_filename(event)}: {link}"
    elif msgtype in ("m.text", "m.emote") and body.strip():
        text = body.strip()
    else:
        await _notice(app, room_id, "That kind of message can't be sent in a Bluesky chat.")
        return True

    # A caption's links are found as for a text message; the file's own
    # link added after it is a bare address either way.
    parts = rich_parts(text, content, _MAX_MESSAGE_LENGTH)
    if len(parts) > _MAX_MESSAGE_PARTS:
        await _notice(app, room_id, "That message is too long to send as a chat.")
        return True
    try:
        for index, (part, facets) in enumerate(parts):
            message: dict[str, Any] = {"text": part}
            if facets:
                message["facets"] = facets
            if reply_to and index == 0:
                message["replyTo"] = {"messageId": reply_to}
            sent = await chat_call(
                app, account, "chat.bsky.convo.sendMessage", body={"convoId": chat.convo_id, "message": message}
            )
            # Recorded so the service's own report of this message isn't
            # mirrored back, and so redacting the event can delete it.
            await repository.add_chat_message(str(sent.get("id", "")), room_id, event_id if index == 0 else f"{event_id}#{index}")
    except ChatError as exc:
        await _notice(app, room_id, f"Couldn't send that: {exc}")
    return True


async def maybe_send_chat_reaction(request: Request, event: dict) -> bool:
    """Returns True if this was the local user reacting to a message in
    one of their chat rooms (sent on as a chat reaction)."""
    if event.get("type") != "m.reaction":
        return False
    app = request.app
    repository = app.state.repository
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    chat = await repository.get_chat_room_for_room(room_id)
    if chat is None:
        return False
    relates_to = (event.get("content") or {}).get("m.relates_to") or {}
    key = str(relates_to.get("key") or "")
    account = await repository.get_account(chat.local_did)
    message_id = await repository.get_chat_message_for_event(str(relates_to.get("event_id") or ""))
    if account is None or event.get("sender") != account.matrix_user_id or not message_id or not key or not event_id:
        return True
    if await repository.get_value(f"chat_reaction:{event_id}") is not None:
        return True  # a transaction Synapse delivered twice
    try:
        await chat_call(
            app, account, "chat.bsky.convo.addReaction", body={"convoId": chat.convo_id, "messageId": message_id, "value": key}
        )
    except ChatError as exc:
        # E.g. not a single emoji, which is all a chat reaction can be.
        await _notice(app, room_id, f"Couldn't send that reaction: {exc}")
        return True
    # Remembered so removing the reaction in Matrix can remove it there,
    # and so the service's own report of it isn't mirrored back.
    await repository.set_value(f"chat_reaction:{event_id}", f"{message_id}|{key}")
    return True


def _incoming_reaction_key(message_id: str, sender_did: str, value: str) -> str:
    return f"chat_reaction_in:{message_id}|{sender_did}|{value}"


async def _mirror_incoming_reaction(app: FastAPI, account: Account, entry: dict[str, Any], *, added: bool) -> None:
    """The other person reacted to (or un-reacted from) a chat message."""
    repository = app.state.repository
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    reaction = entry.get("reaction") if isinstance(entry.get("reaction"), dict) else {}
    sender_did = str((reaction.get("sender") or {}).get("did", ""))
    value = str(reaction.get("value") or "")
    message_id = str(message.get("id", ""))
    if not sender_did or not value or sender_did == account.did:
        return  # our own reaction, reported back
    target = await repository.get_event_for_chat_message(message_id)
    if target is None:
        return
    key = _incoming_reaction_key(message_id, sender_did, value)
    existing = await repository.get_value(key)
    try:
        if added and existing is None:
            event_id = await app.state.synapse.send_reaction(
                target[0], target[1].split("#", 1)[0], value, as_user_id=ghost_user_id(app, sender_did)
            )
            await repository.set_value(key, event_id)
        elif not added and existing is not None:
            await app.state.synapse.redact_event(target[0], existing, as_user_id=_bot(app))
            await repository.delete_value(key)
    except SynapseError:
        logger.info("Could not mirror chat reaction on %s", message_id, exc_info=True)


async def maybe_handle_chat_redaction(request: Request, event: dict) -> bool:
    """Returns True if this redacted a message sent in a chat room. Chat
    only lets you delete a message for yourself, so that is what happens:
    the other person keeps their copy."""
    if event.get("type") != "m.room.redaction":
        return False
    app = request.app
    repository = app.state.repository
    redacted_id = event.get("redacts") or (event.get("content") or {}).get("redacts")
    chat = await repository.get_chat_room_for_room(event.get("room_id", ""))
    if chat is None or not redacted_id:
        return False
    account = await repository.get_account(chat.local_did)
    reaction = await repository.get_value(f"chat_reaction:{redacted_id}")
    if reaction is not None:
        # A reaction of the user's own, removed.
        reacted_message, _, value = reaction.partition("|")
        if account is not None:
            try:
                await chat_call(
                    app,
                    account,
                    "chat.bsky.convo.removeReaction",
                    body={"convoId": chat.convo_id, "messageId": reacted_message, "value": value},
                )
            except ChatError as exc:
                logger.info("Could not remove chat reaction: %s", exc)
        await repository.delete_value(f"chat_reaction:{redacted_id}")
        return True
    message_id = await repository.get_chat_message_for_event(redacted_id)
    if not message_id:
        return False
    if account is not None:
        try:
            await chat_call(
                app, account, "chat.bsky.convo.deleteMessageForSelf", body={"convoId": chat.convo_id, "messageId": message_id}
            )
        except ChatError as exc:
            logger.info("Could not delete chat message %s: %s", message_id, exc)
    return True


def _sent_at_ms(message: dict[str, Any]) -> int | None:
    try:
        return int(datetime.fromisoformat(str(message.get("sentAt", "")).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


async def _mirror_incoming(app: FastAPI, account: Account, convo_id: str, message: dict[str, Any]) -> None:
    repository = app.state.repository
    message_id = str(message.get("id", ""))
    sender_did = str((message.get("sender") or {}).get("did", ""))
    if not message_id or not sender_did or sender_did == account.did:
        return  # our own message, reported back
    if await repository.get_event_for_chat_message(message_id) is not None:
        return
    chat = await repository.get_chat_room_for_convo(account.did, convo_id)
    if chat is None:
        identity = await resolve_did(app.state.http_client, sender_did, plc_directory=app.state.config.atproto.plc_directory)
        if identity is None:
            return
        room_id = await ensure_chat_room(app, account, identity, convo_id)
    else:
        room_id = chat.room_id
    ghost = ghost_user_id(app, sender_did)
    text = str(message.get("text") or "")
    plain = text
    formatted = render_text(text, message.get("facets"))
    record_uri = str(((message.get("embed") or {}).get("record") or {}).get("uri", ""))
    parts = record_uri.removeprefix("at://").split("/")
    if len(parts) == 3:
        # A post shared into the chat.
        url = post_url(parts[0], parts[2])
        plain = f"{plain}\n\n{url}".strip()
        formatted = f'{formatted}<br><br><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
    if not plain:
        return
    content: dict[str, Any] = {"msgtype": "m.text", "body": plain, "format": "org.matrix.custom.html", "formatted_body": formatted}
    # A reply on Bluesky is a reply here, if what it answers is in the room.
    replied = message.get("replyTo") if isinstance(message.get("replyTo"), dict) else {}
    replied_event = await repository.get_event_for_chat_message(str(replied.get("id", ""))) if replied.get("id") else None
    if replied_event is not None and replied_event[0] == room_id:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": replied_event[1].split("#", 1)[0]}}
    try:
        event_id = await app.state.synapse.send_message_event(room_id, content, as_user_id=ghost, ts=_sent_at_ms(message))
    except SynapseError:
        logger.warning("Could not mirror chat message %s into %s", message_id, room_id, exc_info=True)
        return
    await repository.add_chat_message(message_id, room_id, event_id)


async def poll_chats(app: FastAPI, account: Account) -> None:
    """Fetch and mirror what has happened in ``account``'s chats since
    last time. The first check of an account only notes the position."""
    repository = app.state.repository
    key = f"chat_cursor:{account.did}"
    cursor = await repository.get_value(key)
    data = await chat_call(app, account, "chat.bsky.convo.getLog", params={"cursor": cursor} if cursor else None)
    if cursor is not None:
        for entry in data.get("logs") or []:
            kind = str(entry.get("$type", ""))
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            if kind.endswith("#logCreateMessage"):
                await _mirror_incoming(app, account, str(entry.get("convoId", "")), message)
            elif kind.endswith(("#logAddReaction", "#logRemoveReaction")):
                await _mirror_incoming_reaction(app, account, entry, added=kind.endswith("#logAddReaction"))
            elif kind.endswith("#logDeleteMessage"):
                mirrored = await repository.get_event_for_chat_message(str(message.get("id", "")))
                if mirrored is not None and str((message.get("sender") or {}).get("did", "")) != account.did:
                    try:
                        await app.state.synapse.redact_event(mirrored[0], mirrored[1], as_user_id=_bot(app))
                    except SynapseError:
                        logger.info("Could not redact chat message in %s", mirrored[0], exc_info=True)
    if data.get("cursor"):
        await repository.set_value(key, str(data["cursor"]))


async def chat_loop(app: FastAPI) -> None:
    interval = app.state.config.atproto.chat_poll_seconds
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            for account in await app.state.repository.list_accounts():
                if account.plc_registered and account.active:
                    try:
                        await poll_chats(app, account)
                    except ChatError as exc:
                        logger.info("Chat check for %s: %s", account.did, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat check failed")
