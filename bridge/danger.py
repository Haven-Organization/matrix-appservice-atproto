"""The Danger Zone: commands that change what a room IS to the bridge.

``link profile``, ``unlink profile``, ``replace room``, ``replace
dm``/``replace chat`` and ``delete profile`` manipulate the binding
between a Matrix room and an ATProto identity -- which room an identity
lives in, or whether it exists at all. Used on the wrong room they can
leave things inconsistent, and one of them cannot be undone.

So every one is confirmation-gated, the same way as in
matrix-appservice-activitypub: running the command only checks that it
would be allowed and posts a warning saying exactly what will happen.
Nothing changes until the same person replies "confirm" to that specific
message.

The pending action is carried on the warning message itself (in
``CONFIRM_FIELD``), not in the database: a confirmation is recognized by
looking up what was replied to. A warning nobody answers leaves no state
behind, and one that is answered re-checks everything before acting.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from bridge.admins import is_admin
from bridge.atproto.identity import IdentityError, RemoteIdentity, resolve_account, resolve_did
from bridge.chat import ensure_chat_room, ensure_declaration
from bridge.commands import _first_line, bot_mxid, notice, reply_target_event_id
from bridge.follows import invite_to_room
from bridge.handle_provisioning import HandleRejected
from bridge.onboarding import ensure_user_rooms
from bridge.pds import PdsError
from bridge.profile_rooms import bind_profile_room, create_profile_room
from bridge.remote_accounts import create_remote_room, ensure_ghost
from bridge.repository import RemoteAccount
from bridge.spaces import NOTIFICATIONS_ROOM_SPACE_ORDER, add_room_to_space, remove_room_from_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# On the bot's warning message: {"action": ..., "user": who may confirm, "args": {...}}.
CONFIRM_FIELD = "software.haven.atproto.confirm"

_REPLACED_SUFFIX_RE = re.compile(r" \(Replaced \d{2}/\d{2}/\d{4}\)$")
_DELETED_SUFFIX = " (Deleted)"

_KIND_LABELS = {
    "profile": "Profile Room",
    "remote": "Remote User Room",
    "chat": "Chat room",
    "notifications": "notifications room",
}


async def _warn(request: Request, room_id: str, *, action: str, user: str, args: dict[str, Any], headline: str, steps: list[str]) -> None:
    """Post the warning that a later "confirm" reply acts on."""
    config = request.app.state.config
    body = f"⚠️ {headline}\n" + "\n".join(f"- {step}" for step in steps) + '\n\nReply to THIS message with "confirm" to go ahead.'
    formatted = (
        f"<p>⚠️ {html.escape(headline)}</p><ul>"
        + "".join(f"<li>{html.escape(step)}</li>" for step in steps)
        + '</ul><p>Reply to THIS message with "confirm" to go ahead.</p>'
    )
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
        CONFIRM_FIELD: {"action": action, "user": user, "args": args},
    }
    try:
        await request.app.state.synapse.send_message_event(room_id, content, as_user_id=bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send %s warning to %s", action, room_id, exc_info=True)


async def maybe_handle_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this was a "confirm" reply to one of the bot's own
    warnings (handled, whether or not it went through). Only the person
    the warning was addressed to can confirm it; anyone else's "confirm"
    is claimed and ignored."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    if _first_line(content).strip().lower() != "confirm":
        return False
    target = reply_target_event_id(content)
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not target or not room_id or not sender:
        return False
    config = request.app.state.config
    bot = bot_mxid(config)
    try:
        warning = await request.app.state.synapse.get_event(room_id, target, as_user_id=bot)
    except SynapseError:
        return False
    pending = ((warning or {}).get("content") or {}).get(CONFIRM_FIELD)
    if (warning or {}).get("sender") != bot or not isinstance(pending, dict):
        return False
    if pending.get("user") != sender:
        return True
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    executor = _EXECUTORS.get(str(pending.get("action")))
    if executor is not None:
        await executor(request, sender=sender, room_id=room_id, args=args)
    return True


# -- shared -------------------------------------------------------------------


async def _room_kinds(request: Request, room_id: str) -> list[str]:
    """Everything the bridge uses ``room_id`` for. More than one entry
    means its bookkeeping is already inconsistent."""
    repository = request.app.state.repository
    kinds = []
    if await repository.get_account_for_profile_room(room_id) is not None:
        kinds.append("profile")
    if await repository.get_remote_account_for_room(room_id) is not None:
        kinds.append("remote")
    if await repository.get_chat_room_for_room(room_id) is not None:
        kinds.append("chat")
    if await repository.get_notifications_room_owner(room_id) is not None:
        kinds.append("notifications")
    if await repository.get_space_owner(room_id) is not None:
        kinds.append("space")
    return kinds


async def _sender_controls_room(request: Request, room_id: str, sender: str) -> bool:
    """Whether ``sender`` could set the room's name themselves -- the bar
    for being trusted to make it their identity. Fails closed if the
    power levels can't be read."""
    try:
        levels = await request.app.state.synapse.get_room_state(
            room_id, "m.room.power_levels", as_user_id=bot_mxid(request.app.state.config)
        )
    except SynapseError:
        return False
    if levels is None:
        return False
    sender_level = (levels.get("users") or {}).get(sender, levels.get("users_default", 0))
    required = (levels.get("events") or {}).get("m.room.name", levels.get("state_default", 50))
    return sender_level >= required


async def _predecessor(request: Request, room_id: str) -> dict[str, str] | None:
    event_id = await request.app.state.synapse.get_last_event_id(room_id, as_user_id=bot_mxid(request.app.state.config))
    return {"room_id": room_id, "event_id": event_id} if event_id else None


async def _retire_room(request: Request, *, old_room_id: str, new_room_id: str, as_user_id: str) -> None:
    """Tombstone the old room, pointing at its replacement, and rename it
    so a stale room-list entry is unmistakable. Best-effort."""
    synapse = request.app.state.synapse
    try:
        await synapse.send_state_event(
            old_room_id,
            "m.room.tombstone",
            {"body": "This room has been replaced", "replacement_room": new_room_id},
            as_user_id=as_user_id,
        )
    except SynapseError:
        logger.info("Could not tombstone %s", old_room_id, exc_info=True)
    try:
        name = ((await synapse.get_room_state(old_room_id, "m.room.name", as_user_id=as_user_id)) or {}).get("name")
        if name and not _REPLACED_SUFFIX_RE.search(name):
            suffix = f" (Replaced {datetime.now(timezone.utc).strftime('%m/%d/%Y')})"
            await synapse.send_state_event(old_room_id, "m.room.name", {"name": f"{name}{suffix}"}, as_user_id=as_user_id)
    except SynapseError:
        logger.info("Could not rename replaced room %s", old_room_id, exc_info=True)


async def _local_members(request: Request, room_id: str) -> list[str]:
    """The real (non-bridge) local users in a room."""
    config = request.app.state.config
    try:
        members = await request.app.state.synapse.get_joined_members(room_id, as_user_id=bot_mxid(config))
    except SynapseError:
        return []
    prefix = f"@{config.appservice.user_prefix}"
    return [
        m for m in members
        if m != bot_mxid(config) and not m.startswith(prefix) and m.endswith(f":{config.synapse.server_name}")
    ]


# -- unlink profile -----------------------------------------------------------


async def handle_unlink_profile(request: Request, *, sender: str, room_id: str) -> None:
    account = await request.app.state.repository.get_account_for_user(sender)
    if account is None or not account.profile_room_id:
        await notice(request, room_id, "You don't have a linked Profile Room to unlink.")
        return
    handle = request.app.state.pds.handle_of(account)
    p = request.app.state.config.appservice.command_prefix
    await _warn(
        request,
        room_id,
        action="unlink_profile",
        user=sender,
        args={},
        headline=f"This will detach your Profile Room from your Bluesky identity ({handle}).",
        steps=[
            "The room stops publishing for you immediately: nothing you post there goes to Bluesky.",
            f'It becomes linkable as a different identity by anyone with enough power there who runs "{p}link profile".',
            "Your identity itself is untouched: your handle, DID, existing posts, follows and followers all stay.",
            f'To move your profile, run "{p}link profile" in the room you want (or "{p}create profile" for a new one).',
        ],
    )


async def _execute_unlink_profile(request: Request, *, sender: str, room_id: str, args: dict) -> None:
    repository = request.app.state.repository
    account = await repository.get_account_for_user(sender)
    if account is None or not account.profile_room_id:
        await notice(request, room_id, "You don't have a linked Profile Room to unlink.")
        return
    await repository.clear_profile_room(account.did)
    p = request.app.state.config.appservice.command_prefix
    await notice(
        request,
        room_id,
        f'Unlinked. {request.app.state.pds.handle_of(account)} no longer has a Profile Room -- run "{p}link profile" '
        f'in the room you want, or "{p}create profile" for a new one.',
    )
    logger.info("%s unlinked Profile Room %s", sender, account.profile_room_id)


# -- link profile -------------------------------------------------------------


async def _link_refusal(request: Request, *, sender: str, room_id: str) -> str | None:
    """Why ``sender`` may not link ``room_id``, or ``None`` if they may."""
    app = request.app
    repository = app.state.repository
    p = app.state.config.appservice.command_prefix
    account = await repository.get_account_for_user(sender)
    if account is not None and account.profile_room_id:
        return f'You already have a Profile Room. Run "{p}unlink profile" first if you want to move it here.'
    kinds = await _room_kinds(request, room_id)
    if kinds:
        return "This room is already used by the bridge for something else, so it can't become a Profile Room."
    if not await _sender_controls_room(request, room_id, sender):
        return "You need enough power in this room to change its name before you can link it."
    try:
        if await app.state.synapse.get_room_state(room_id, "m.room.encryption", as_user_id=bot_mxid(app.state.config)):
            return "This room is encrypted, so I couldn't read what you post in it."
    except SynapseError:
        return "I couldn't check this room's settings."
    return None


async def handle_link_profile(request: Request, *, sender: str, room_id: str) -> None:
    refusal = await _link_refusal(request, sender=sender, room_id=room_id)
    if refusal is not None:
        await notice(request, room_id, refusal)
        return
    await _warn(
        request,
        room_id,
        action="link_profile",
        user=sender,
        args={},
        headline="This will make this room your permanent Bluesky profile.",
        steps=[
            "Everything you post here from then on is published publicly on Bluesky.",
            "This room's name, topic and avatar become your display name, bio and avatar.",
            "If you don't have a Bluesky identity yet, one is created and registered -- publicly and permanently.",
            "This is a lasting change to what this room IS, not a one-off action.",
        ],
    )


async def _execute_link_profile(request: Request, *, sender: str, room_id: str, args: dict) -> None:
    app = request.app
    refusal = await _link_refusal(request, sender=sender, room_id=room_id)
    if refusal is not None:
        await notice(request, room_id, refusal)
        return
    try:
        account = await app.state.pds.create_account(sender)
    except HandleRejected as exc:
        await notice(request, room_id, f"Can't create your Bluesky identity: {exc.rejection.message}")
        return
    except PdsError as exc:
        logger.warning("link profile for %s failed: %s", sender, exc.message)
        await notice(request, room_id, "Couldn't register your identity just now -- please try again in a minute.")
        return
    account = await bind_profile_room(app, account, room_id)
    await ensure_user_rooms(app, account)
    try:
        await ensure_declaration(app, account)
    except PdsError:
        logger.warning("Could not publish chat declaration for %s", account.did, exc_info=True)
    await notice(request, room_id, f"Linked. This room is now the Profile Room of {app.state.pds.handle_of(account)}.")
    logger.info("%s linked %s as their Profile Room", sender, room_id)


# -- replace room -------------------------------------------------------------


async def _replace_permission(request: Request, *, kind: str, room_id: str, sender: str) -> bool:
    """Your own Profile Room, chat or notifications room: you or an
    admin. Someone else's Remote User Room: an admin only."""
    repository = request.app.state.repository
    if await is_admin(request.app, sender):
        return True
    if kind == "profile":
        account = await repository.get_account_for_profile_room(room_id)
        return account is not None and account.matrix_user_id == sender
    if kind == "chat":
        chat = await repository.get_chat_room_for_room(room_id)
        account = await repository.get_account(chat.local_did) if chat else None
        return account is not None and account.matrix_user_id == sender
    if kind == "notifications":
        return await repository.get_notifications_room_owner(room_id) == sender
    return False


async def _classify_for_replace(request: Request, *, room_id: str, sender: str, reply_room_id: str) -> str | None:
    """The single replaceable kind ``room_id`` is, or ``None`` after
    saying why it can't be replaced by ``sender``."""
    kinds = await _room_kinds(request, room_id)
    if len(kinds) > 1:
        # Already inconsistent: replacing it as one thing would compound that.
        await notice(
            request,
            reply_room_id,
            "This room is registered with the bridge as more than one kind of room at once, which shouldn't happen. "
            "I won't replace it -- please contact a server admin.",
        )
        return None
    if not kinds or kinds[0] not in _KIND_LABELS:
        await notice(request, reply_room_id, "This isn't a room I can replace.")
        return None
    if not await _replace_permission(request, kind=kinds[0], room_id=room_id, sender=sender):
        who = "a server admin" if kinds[0] == "remote" else "its owner or a server admin"
        await notice(request, reply_room_id, f"Only {who} can replace this room.")
        return None
    return kinds[0]


async def handle_replace_room(request: Request, *, sender: str, room_id: str) -> None:
    kind = await _classify_for_replace(request, room_id=room_id, sender=sender, reply_room_id=room_id)
    if kind is None:
        return
    await _warn(
        request,
        room_id,
        action="replace_room",
        user=sender,
        args={"room_id": room_id},
        headline=f"This will create a new room and tombstone this one ({_KIND_LABELS[kind]}).",
        steps=[
            "A fresh room is created for the same identity, brought up to date with how the bridge sets rooms up now.",
            'This room is tombstoned (pointing at the new one) and renamed with a "(Replaced ...)" suffix.',
            "Local members are invited to the new room. Nobody is removed from this one.",
            "Nothing changes on Bluesky: the identity behind the room is the same.",
        ],
    )


async def handle_replace_chat(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """``replace dm``/``replace chat <handle>``: runnable from anywhere,
    for a chat room the bot can't read commands in (one that ended up
    encrypted, say)."""
    app = request.app
    repository = app.state.repository
    p = app.state.config.appservice.command_prefix
    account = await repository.get_account_for_user(sender)
    if account is None:
        await notice(request, room_id, f'You need a Bluesky identity first -- run "{p}create profile".')
        return
    if not argument.strip():
        await notice(request, room_id, f"Usage: {p}replace chat handle.example.com")
        return
    try:
        identity = await resolve_account(
            app.state.http_client, argument.split()[0], plc_directory=app.state.config.atproto.plc_directory
        )
    except IdentityError as exc:
        await notice(request, room_id, str(exc))
        return
    chat = await repository.get_chat_room(account.did, identity.did)
    if chat is None:
        await notice(request, room_id, f"You don't have a chat room with @{identity.handle}.")
        return
    await _warn(
        request,
        room_id,
        action="replace_room",
        user=sender,
        args={"room_id": chat.room_id},
        headline=f"This will replace your existing Chat room with @{identity.handle}.",
        steps=[
            "A fresh room is created for the same chat and you are invited to it.",
            'The old room is tombstoned (pointing at the new one) and renamed with a "(Replaced ...)" suffix.',
            "Nothing changes on Bluesky: it is the same conversation.",
        ],
    )


async def _identity_for(request: Request, did: str, fallback: RemoteAccount | None = None) -> RemoteIdentity | None:
    app = request.app
    identity = await resolve_did(app.state.http_client, did, plc_directory=app.state.config.atproto.plc_directory)
    if identity is None and fallback is not None:
        identity = RemoteIdentity(fallback.did, fallback.handle, fallback.pds)
    return identity


async def _execute_replace_room(request: Request, *, sender: str, room_id: str, args: dict) -> None:
    """``room_id`` is where the confirmation was given; ``args["room_id"]``
    is the room to replace (the same, except for ``replace chat``)."""
    app = request.app
    repository = app.state.repository
    synapse = app.state.synapse
    config = app.state.config
    bot = bot_mxid(config)
    old_room_id = str(args.get("room_id") or room_id)
    kind = await _classify_for_replace(request, room_id=old_room_id, sender=sender, reply_room_id=room_id)
    if kind is None:
        return
    predecessor = await _predecessor(request, old_room_id)
    retire_as = bot
    try:
        if kind == "profile":
            account = await repository.get_account_for_profile_room(old_room_id)
            assert account is not None
            name = ((await synapse.get_room_state(old_room_id, "m.room.name", as_user_id=bot)) or {}).get("name")
            topic = ((await synapse.get_room_state(old_room_id, "m.room.topic", as_user_id=bot)) or {}).get("topic")
            avatar = ((await synapse.get_room_state(old_room_id, "m.room.avatar", as_user_id=bot)) or {}).get("url")
            members = sorted({account.matrix_user_id, *await _local_members(request, old_room_id)})
            account = await create_profile_room(
                app,
                account,
                name=name or app.state.pds.handle_of(account),
                avatar_mxc=avatar,
                topic=topic,
                invite=members,
                predecessor=predecessor,
            )
            new_room_id = account.profile_room_id or ""
            await ensure_user_rooms(app, account)
        elif kind == "remote":
            remote = await repository.get_remote_account_for_room(old_room_id)
            assert remote is not None
            identity = await _identity_for(request, remote.did, remote)
            assert identity is not None
            ghost, label, description, avatar_mxc, profile = await ensure_ghost(app, identity)
            members = await _local_members(request, old_room_id)
            new_room_id = await create_remote_room(
                app, identity, ghost, label=label, description=description, avatar_mxc=avatar_mxc,
                profile=profile, predecessor=predecessor,
            )
            await repository.save_remote_account(RemoteAccount(identity.did, identity.handle, identity.pds, ghost, new_room_id))
            for member in members:
                await invite_to_room(app, new_room_id, member)
                await add_room_to_space(app, member, new_room_id)
                await remove_room_from_space(app, member, old_room_id)
        elif kind == "chat":
            chat = await repository.get_chat_room_for_room(old_room_id)
            assert chat is not None
            account = await repository.get_account(chat.local_did)
            identity = await _identity_for(request, chat.remote_did)
            if account is None or identity is None:
                await notice(request, room_id, "Couldn't look up who this chat is with.")
                return
            new_room_id = await ensure_chat_room(
                app, account, identity, chat.convo_id, predecessor=predecessor, replace=True
            )
            await remove_room_from_space(app, account.matrix_user_id, old_room_id)
            # The ghost created the old room, so it is the one sure to
            # have the power to tombstone it.
            retire_as = (await ensure_ghost(app, identity))[0]
        else:  # notifications
            owner = await repository.get_notifications_room_owner(old_room_id)
            assert owner is not None
            new_room_id = await synapse.create_room(
                as_user_id=bot,
                name=config.bridge.notifications_room_name,
                invite=[owner],
                is_direct=True,
                preset="trusted_private_chat",
                avatar_mxc=config.bridge.notifications_room_avatar_mxc or config.appservice.bot_avatar_mxc,
                predecessor=predecessor,
            )
            await repository.set_notifications_room(owner, new_room_id)
            await remove_room_from_space(app, owner, old_room_id)
            await add_room_to_space(app, owner, new_room_id, order=NOTIFICATIONS_ROOM_SPACE_ORDER)
    except SynapseError:
        logger.warning("Replacing %s (%s) failed", old_room_id, kind, exc_info=True)
        await notice(request, room_id, "Couldn't create the replacement room, so nothing was changed.")
        return
    await _retire_room(request, old_room_id=old_room_id, new_room_id=new_room_id, as_user_id=retire_as)
    link = f"https://matrix.to/#/{new_room_id}?via={config.synapse.server_name}"
    await notice(
        request,
        room_id,
        f"Replaced. The new {_KIND_LABELS[kind]} is {new_room_id} ({link}).",
        html_message=f'Replaced. The new {_KIND_LABELS[kind]} is <a href="{html.escape(link, quote=True)}">{html.escape(new_room_id)}</a>.',
    )
    logger.info("%s replaced %s %s with %s", sender, kind, old_room_id, new_room_id)


# -- delete profile -----------------------------------------------------------


async def handle_delete_profile(request: Request, *, sender: str, room_id: str) -> None:
    account = await request.app.state.repository.get_account_for_user(sender)
    if account is None:
        await notice(request, room_id, "You don't have a Bluesky identity to delete.")
        return
    handle = request.app.state.pds.handle_of(account)
    await _warn(
        request,
        room_id,
        action="delete_profile",
        user=sender,
        args={},
        headline=f"This will permanently delete your Bluesky identity ({handle}). This cannot be undone.",
        steps=[
            "Tell the Bluesky network the account is deleted, so your profile and every post disappear there.",
            "Retire the account's DID permanently, so it can never be used or recovered.",
            "Remove you from every other Bluesky room the bridge put you in (accounts you follow, chats, notifications).",
            "Remove you from your space.",
            'Unlink your Profile Room and rename it to end in " (Deleted)". The room itself is left for you to leave when you like.',
            "Erase the identity here: its signing key, follows, and all record of its posts.",
            f"Free the handle {handle}: creating a profile again later gives you a brand new, different account.",
        ],
    )


async def _execute_delete_profile(request: Request, *, sender: str, room_id: str, args: dict) -> None:
    app = request.app
    repository = app.state.repository
    synapse = app.state.synapse
    bot = bot_mxid(app.state.config)
    account = await repository.get_account_for_user(sender)
    if account is None:
        await notice(request, room_id, "You don't have a Bluesky identity to delete.")
        return
    handle = app.state.pds.handle_of(account)

    # Everything the bridge put them in, except the Profile Room itself.
    rooms = [remote.room_id for remote in await repository.list_follows(account.did)]
    rooms += [chat.room_id for chat in await repository.list_chat_rooms(account.did)]
    for extra in (await repository.get_notifications_room(sender), await repository.get_user_space(sender)):
        if extra:
            rooms.append(extra)
    profile_room_id = account.profile_room_id

    await app.state.pds.delete_account(account.did)
    app.state.jetstream.refresh()

    for other in rooms:
        if other == profile_room_id:
            continue
        try:
            await synapse.kick_user(other, sender, as_user_id=bot, reason="Bluesky identity deleted")
        except SynapseError:
            pass  # not in it, or (a chat room) the bot lacks the power to
    if profile_room_id:
        try:
            name = ((await synapse.get_room_state(profile_room_id, "m.room.name", as_user_id=bot)) or {}).get("name")
            if name and not name.endswith(_DELETED_SUFFIX):
                await synapse.send_state_event(
                    profile_room_id, "m.room.name", {"name": f"{name}{_DELETED_SUFFIX}"}, as_user_id=bot
                )
        except SynapseError:
            logger.info("Could not rename deleted Profile Room %s", profile_room_id, exc_info=True)
    await notice(request, room_id, f"Deleted. {handle} no longer exists.")
    logger.info("%s deleted their identity %s", sender, handle)


_EXECUTORS = {
    "unlink_profile": _execute_unlink_profile,
    "link_profile": _execute_link_profile,
    "replace_room": _execute_replace_room,
    "delete_profile": _execute_delete_profile,
}
