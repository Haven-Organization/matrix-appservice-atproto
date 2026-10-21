"""Bot commands: how a Matrix user sets up and drives their ATProto identity.

Same structure as matrix-appservice-activitypub's commands. A message is a
command when it either

- tags the bot (a Matrix mention, or its MXID typed out) followed by the
  command -- ``@bluesky:example.org create profile`` -- or
- starts with the command prefix immediately followed by a command --
  ``]create profile`` (``appservice.command_prefix``; the ActivityPub
  bridge uses ``;``, so the two can share a room without both answering).

Only the first line is looked at, and matching is case-insensitive.
Anything that addresses the bot is consumed here -- recognized or not --
so that a mistyped command sent in a Profile Room is never published as a
post.

Commands are grouped into sections; ``help`` lists the sections and
``help <section>`` one section's commands. ``_help_sections`` and
COMMANDS.md follow the same breakdown -- keep both in sync when adding or
moving a command.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from fastapi import Request

from bridge.atproto.identity import (
    IdentityError,
    RemoteIdentity,
    get_record,
    list_all_records,
    parse_account_reference,
    resolve_account,
    resolve_did,
)
from bridge.follows import FOLLOW_COLLECTION, follow, invite_to_room, now_timestamp, run_in_background, unfollow
from bridge.handle_provisioning import HandleRejected
from bridge.matrix_records import POST_COLLECTION
from bridge.onboarding import ensure_user_rooms
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
from bridge.pds import PdsError, Write
from bridge.post_mirroring import mirror_post
from bridge.profile_sync import sync_profile_record
from bridge.admins import is_admin
from bridge.chat import ChatError, ensure_declaration, start_chat
from bridge.profile_rooms import create_profile_room
from bridge.remote_accounts import (
    backfill,
    backfill_thread,
    ensure_remote_account,
    refresh_remote_account,
    sync_remote_settings,
)
from bridge.spaces import add_room_to_space
from bridge.repository import Account, RemoteAccount
from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

BLOCK_COLLECTION = "app.bsky.graph.block"

# Longer keywords first where one is a prefix of another.
_COMMAND_KEYWORDS = (
    "unfollow|following|follow|unblock|block|backfill|banner|create|import|refresh|repost|status|help|"
    "unlink|link|replace|delete|widget|chat|dm"
)

_COMMAND_RE = re.compile(rf"\b({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE)
_POST_URL_RE = re.compile(r"^https?://[^/]+/profile/([^/?#]+)/post/([A-Za-z0-9._:~-]+)")
_AT_POST_URI_RE = re.compile(rf"^at://([^/]+)/{re.escape(POST_COLLECTION)}/([A-Za-z0-9._:~-]+)$")


def bot_mxid(config: Any) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def is_bridge_user(config: Any, user_id: str) -> bool:
    """Whether ``user_id`` is this bridge's bot or one of its ghosts."""
    return user_id == bot_mxid(config) or user_id.startswith(f"@{config.appservice.user_prefix}")


def is_bot_user(config: Any, user_id: str) -> bool:
    """Whether ``user_id`` is a bot or ghost -- this bridge's, or another
    bot's or bridge's listed in the config -- whose messages must never be
    acted on. Other bridges' ghosts are local users, so without this a
    fediverse reply mirrored into a shared Profile Room would be answered
    like a local user's, or could even run commands."""
    return is_bridge_user(config, user_id) or config.appservice.is_other_bot(user_id)


def _prefixed_command_re(prefix: str) -> re.Pattern[str]:
    # Anchored to the start of the line and requiring a known keyword right
    # after the prefix: a prefix with anything else after it is NOT treated
    # as addressing the bot. Tagging the bot is unambiguous; a one-character
    # symbol at the start of a line can easily just be someone's ordinary
    # message, which must not be swallowed.
    return re.compile(rf"^{re.escape(prefix)}\s*({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE)


def reply_target_event_id(content: dict) -> str | None:
    """The event a message is a reply to, if it is one."""
    target = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
    return target if isinstance(target, str) and target else None


def _first_line(content: dict) -> str:
    """The message's first line -- past the quoted "> ..." fallback some
    clients prepend to a reply, which would otherwise be what is read."""
    body = content.get("body")
    if not isinstance(body, str):
        return ""
    lines = body.split("\n")
    if reply_target_event_id(content) is not None:
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return lines[0] if lines else ""


def _extract_prefixed_command(content: dict, config: Any) -> tuple[str, str] | None:
    match = _prefixed_command_re(config.appservice.command_prefix).match(_first_line(content))
    return (match.group(1).lower(), match.group(2).strip()) if match else None


def _tags_bot(content: dict, config: Any) -> bool:
    """A proper Matrix mention of the bot, or its bare MXID in the text
    (some clients never send intentional mentions)."""
    mxid = bot_mxid(config)
    mentioned = (content.get("m.mentions") or {}).get("user_ids") or []
    return mxid in mentioned or mxid in _first_line(content)


def message_for_other_bot(content: dict, config: Any) -> bool:
    """Whether a message is a command for another bot sharing the room:
    its first line is one of their prefixes immediately followed by a word
    and then a space or the end of the line ("]follow x", ";help" -- but not
    "[Update] text" or "; not a command"), or it tags one of them. Such a
    message is never published. Same rule as the ActivityPub and Nostr
    bridges."""
    line = _first_line(content)
    prefixes = config.appservice.other_command_prefixes
    if prefixes and re.match(rf"(?:{'|'.join(map(re.escape, prefixes))})[A-Za-z][\w-]*(?:\s|$)", line):
        return True
    mentioned = (content.get("m.mentions") or {}).get("user_ids") or []
    return any(bot in mentioned or bot in line for bot in config.appservice.other_bot_users)


def message_addresses_bot(content: dict, config: Any) -> bool:
    """Whether a message is directed at the bot. Used to recognize
    commands, and independently by ``bridge.profile_posts`` so that such a
    message is never published as a post."""
    return _tags_bot(content, config) or _extract_prefixed_command(content, config) is not None


async def notice(request: Request, room_id: str, message: str, *, html_message: str | None = None) -> None:
    """Send an ``m.notice`` as the bot. ``message`` is always the plain-text
    form; ``html_message`` adds a formatted one."""
    content: dict[str, Any] = {"msgtype": "m.notice", "body": message}
    if html_message is not None:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html_message
    try:
        await request.app.state.synapse.send_message_event(
            room_id, content, as_user_id=bot_mxid(request.app.state.config)
        )
    except SynapseError:
        logger.warning("Failed to send notice to %s", room_id, exc_info=True)


def _room_link_html(room_id: str) -> str:
    return f'<a href="https://matrix.to/#/{html.escape(room_id)}">{html.escape(room_id)}</a>'


def _profile_url(handle: str) -> str:
    return f"https://bsky.app/profile/{handle}"


async def maybe_handle_command(request: Request, event: dict) -> bool:
    """Returns True if the message addressed the bot (and so is handled,
    whether or not a recognized command was found in it)."""
    if event.get("type") != "m.room.message":
        return False
    config = request.app.state.config
    content = event.get("content") or {}
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    if not sender or not room_id or not message_addresses_bot(content, config):
        return False
    if is_bot_user(config, sender):
        return True

    prefix = config.appservice.command_prefix
    parsed = _extract_prefixed_command(content, config)
    if parsed is None:
        # Tagged: the keyword can be anywhere on the first line.
        match = _COMMAND_RE.search(_first_line(content))
        parsed = (match.group(1).lower(), match.group(2).strip()) if match else None
    if parsed is None:
        # Tagged with no recognizable command: the help overview if that
        # was all they said, a pointer to it if they said something else.
        residue = _first_line(content).replace(bot_mxid(config), "")
        for name in (config.appservice.bot_display_name, config.appservice.bot_localpart):
            if name:
                residue = residue.replace(name, "")
        if residue.strip(" \t@:,"):
            await notice(request, room_id, f'Not sure what you mean -- try "{prefix}help" to see what I can do.')
        else:
            await _handle_help(request, room_id=room_id)
        return True
    keyword, argument = parsed

    if keyword == "help":
        await _handle_help(request, room_id=room_id, argument=argument)
        return True

    # Everything else creates or acts as an identity under this server's
    # domain, so it is only for this homeserver's own users: anyone on any
    # server can invite the bot to a room.
    if sender.split(":", 1)[-1] != config.synapse.server_name:
        await notice(request, room_id, f"Commands are only available to users on {config.synapse.server_name}.")
        return True

    lowered = " ".join(argument.lower().split())
    context = {"sender": sender, "room_id": room_id}
    if keyword == "create" and lowered == "profile":
        await _handle_create_profile(request, **context)
    elif keyword == "status" and not lowered:
        await _handle_status(request, **context)
    elif keyword == "banner":
        await _handle_banner(request, argument=argument, content=content, **context)
    elif keyword == "follow":
        await _handle_follow(request, argument=argument, **context)
    elif keyword == "unfollow":
        await _handle_unfollow(request, argument=argument, **context)
    elif keyword == "following" and not lowered:
        await _handle_following(request, **context)
    elif keyword == "import" and lowered.split()[:1] in (["follows"], ["following"]):
        source = argument.split(None, 1)[1] if len(lowered.split()) > 1 else ""
        await _handle_import_follows(request, argument=source, content=content, **context)
    elif keyword == "import" and argument:
        await _handle_import(request, argument=argument, **context)
    elif keyword == "repost":
        await _handle_repost(request, event=event, argument=argument, **context)
    elif keyword in ("block", "unblock"):
        await _handle_block(request, argument=argument, block=keyword == "block", **context)
    elif keyword == "backfill":
        await _handle_backfill(request, argument=lowered, content=content, **context)
    elif keyword in ("dm", "chat"):
        await _handle_chat(request, argument=argument, **context)
    elif keyword in ("link", "unlink", "delete", "replace"):
        # Imported here: bridge.danger itself builds on this module.
        from bridge import danger

        words = lowered.split()
        if keyword == "link" and lowered == "profile":
            await danger.handle_link_profile(request, **context)
        elif keyword == "unlink" and lowered == "profile":
            await danger.handle_unlink_profile(request, **context)
        elif keyword == "delete" and lowered == "profile":
            await danger.handle_delete_profile(request, **context)
        elif keyword == "replace" and lowered == "room":
            await danger.handle_replace_room(request, **context)
        elif keyword == "replace" and words[:1] in (["dm"], ["chat"]):
            await danger.handle_replace_chat(request, argument=argument.split(None, 1)[1] if len(words) > 1 else "", **context)
        else:
            await notice(request, room_id, f'Not sure what you mean -- try "{prefix}help" to see what I can do.')
    elif keyword == "refresh":
        await _handle_refresh(request, argument=argument, **context)
    elif keyword == "widget" and not lowered:
        await _handle_widget(request, **context)
    else:
        await notice(request, room_id, f'Not sure what you mean -- try "{prefix}help" to see what I can do.')
    return True


# -- help ---------------------------------------------------------------------


def _help_sections(config: Any) -> dict[str, tuple[str, str, list[tuple[str, str]]]]:
    """Every ``help`` section, keyed by the word that selects it --
    ``(display name, one-line blurb, [(command, description), ...])``.
    Matches COMMANDS.md's section breakdown 1:1."""
    p = config.appservice.command_prefix
    return {
        "profile": (
            "Profile",
            "Setting up and maintaining your own Bluesky identity.",
            [
                (
                    f"{p}create profile",
                    f"Set up a new Bluesky identity (<your username>.{config.bridge.domain}) and a Matrix room "
                    "to post from, in one step.",
                ),
                (f"{p}status", "Show your handle, DID and Profile Room."),
                (
                    f"{p}import follows [handle.example.com]",
                    "Follow every account another Bluesky account follows -- your old account, say. Or reply with "
                    "it to an uploaded file listing handles or DIDs, one per line. Runs in the background and "
                    "posts a summary when done.",
                ),
                (
                    f"{p}banner mxc://server/mediaid",
                    'Set your profile\'s banner image from already-uploaded Matrix media, or reply to an image '
                    f'with just "{p}banner". "{p}banner remove" clears it.',
                ),
            ],
        ),
        "general": (
            "General",
            "Following, chat and everyday commands.",
            [
                (f"{p}help", f'Show the sections. "{p}help <section>" shows one section; "{p}help all" shows everything.'),
                (
                    f"{p}follow handle.example.com",
                    "Follow a Bluesky account. Its posts are mirrored into a room you're invited to. With no "
                    "argument inside an account's room, follows that account.",
                ),
                (
                    f"{p}unfollow handle.example.com",
                    "Stop following. With no argument inside an account's room, unfollows that account. "
                    "Leaving the room does the same.",
                ),
                (f"{p}following", "List the accounts you follow."),
                (
                    f"{p}chat handle.example.com",
                    f"Start (or reopen) a Bluesky chat with an account, as a direct-message room. {p}dm is the same "
                    "command. With no argument inside an account's room, chats with that account.",
                ),
                (
                    f"{p}import https://bsky.app/profile/.../post/...",
                    "Bring one specific post into Matrix without following its author.",
                ),
                (
                    f"{p}repost [your text]",
                    "Reply to a post with this to repost it from your account; add text to quote-post it instead. "
                    "Reacting to a post with 🔁 also reposts it, and with 👍 likes it.",
                ),
            ],
        ),
        "advanced": (
            "Advanced/Maintenance",
            "Blocking, refilling rooms, and other things almost nobody needs day to day.",
            [
                (f"{p}block handle.example.com", "Block an account (and unfollow it)."),
                (f"{p}unblock handle.example.com", "Remove a block."),
                (
                    f"{p}backfill [N]",
                    f"Inside an account's room: pull in its most recent {config.atproto.follow_backfill_count} posts; "
                    "as a reply to a mirrored post, that post's replies instead. Already-mirrored posts are skipped. "
                    "Only a server admin can give a custom N.",
                ),
                (
                    f"{p}widget",
                    "Add the bridge's widget to this room: buttons for most of these commands, for clients that "
                    "support Matrix widgets.",
                ),
            ],
        ),
        "admin": (
            "Admin",
            "Bridge-wide maintenance, gated to a server admin.",
            [
                (
                    f"{p}refresh [handle.example.com]",
                    "Re-read an account's handle, name, avatar and bio, and bring its room and ghost "
                    "into line with the current settings. With no argument, the account whose room this is.",
                ),
            ],
        ),
        "danger": (
            "Danger Zone",
            "Commands that change what a room is, or delete your identity. Each asks you to confirm first.",
            [
                (
                    f"{p}link profile",
                    "Make the room you run this in your permanent Profile Room, instead of having one created "
                    "for you. Asks for confirmation.",
                ),
                (
                    f"{p}unlink profile",
                    "Detach your Profile Room from your identity without deleting anything, e.g. to move your "
                    "profile to another room. Asks for confirmation.",
                ),
                (
                    f"{p}replace room",
                    "Replace this room with a freshly-created one for the same purpose and tombstone this one. "
                    "Asks for confirmation.",
                ),
                (
                    f"{p}replace chat handle.example.com",
                    f"Replace your chat room with that account, from anywhere ({p}replace dm is the same). "
                    "Asks for confirmation.",
                ),
                (
                    f"{p}delete profile",
                    "Permanently delete your Bluesky identity and everything published from it. Irreversible -- "
                    "unlike everything else in this section. Asks for confirmation.",
                ),
            ],
        ),
    }


def _render_help_table(intro: str, intro_html: str, commands: list[tuple[str, str]]) -> tuple[str, str]:
    body = intro + "\n\n" + "\n\n".join(f"{cmd}\n  - {desc}" for cmd, desc in commands)
    rows = "".join(f"<tr><td>{html.escape(cmd)}</td><td>{html.escape(desc)}</td></tr>" for cmd, desc in commands)
    formatted_body = (
        f"<p>{intro_html}</p><table><thead><tr><th>Command</th><th>Description</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return body, formatted_body


async def _handle_help(request: Request, *, room_id: str, argument: str = "") -> None:
    """Sent as an ordinary ``m.text``, not ``m.notice``: someone asking
    what the bot can do should get an answer clients don't mute.

    Bare ``help`` (or an unknown section) lists the sections;
    ``help <section>`` shows that section's commands; ``help all`` shows
    every section's commands in one table."""
    config = request.app.state.config
    p = config.appservice.command_prefix
    sections = _help_sections(config)
    key = argument.strip().lower()
    intro = (
        f'Hi, I\'m the Bluesky bridge! Start a message with "{p}" to give me a command (e.g. "{p}help"). '
        f'You can also tag me in place of "{p}":'
    )
    intro_html = (
        f"Hi, I'm the Bluesky bridge! Start a message with <code>{html.escape(p)}</code> to give me a command "
        f"(e.g. <code>{html.escape(p)}help</code>). You can also tag me in place of <code>{html.escape(p)}</code>:"
    )
    if key == "all":
        commands = [command for _name, _blurb, section in sections.values() for command in section]
        body, formatted_body = _render_help_table(intro, intro_html, commands)
    elif key in sections:
        body, formatted_body = _render_help_table(intro, intro_html, sections[key][2])
    else:
        rows = [(f"{p}help {section_key}", blurb) for section_key, (_name, blurb, _cmds) in sections.items()]
        body, formatted_body = _render_help_table(
            intro + f'\n\nRun "{p}help <section>" to see a section\'s own commands.',
            intro_html + f"</p><p>Run <code>{html.escape(p)}help &lt;section&gt;</code> to see a section's own commands.",
            rows,
        )
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html", "formatted_body": formatted_body}
    try:
        await request.app.state.synapse.send_message_event(room_id, content, as_user_id=bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send help message to %s", room_id, exc_info=True)


# -- shared helpers -----------------------------------------------------------


async def _require_account(request: Request, *, sender: str, room_id: str) -> Account | None:
    """The sender's account, or ``None`` after telling them they need one."""
    account = await request.app.state.repository.get_account_for_user(sender)
    if account is None:
        prefix = request.app.state.config.appservice.command_prefix
        await notice(request, room_id, f'You need a Bluesky identity first -- run "{prefix}create profile".')
    return account


async def _resolve_target(request: Request, *, room_id: str, argument: str, usage: str) -> RemoteIdentity | None:
    """Who a command is about: the account named in ``argument``, or with
    no argument the account whose Remote User Room this is. ``None`` after
    explaining, if neither works out."""
    app = request.app
    config = app.state.config
    plc = config.atproto.plc_directory
    if not argument.strip():
        remote = await app.state.repository.get_remote_account_for_room(room_id)
        if remote is None:
            await notice(request, room_id, f"Usage: {config.appservice.command_prefix}{usage}")
            return None
        # Re-read the identity: the handle or PDS may have changed.
        fresh = await resolve_did(app.state.http_client, remote.did, plc_directory=plc)
        return fresh or RemoteIdentity(remote.did, remote.handle, remote.pds)
    try:
        return await resolve_account(app.state.http_client, argument.split()[0], plc_directory=plc)
    except IdentityError as exc:
        await notice(request, room_id, str(exc))
        return None


async def _is_local(request: Request, identity: RemoteIdentity) -> bool:
    return await request.app.state.repository.get_account(identity.did) is not None


# -- profile ------------------------------------------------------------------


async def _handle_status(request: Request, *, sender: str, room_id: str) -> None:
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    handle = request.app.state.pds.handle_of(account)
    room = account.profile_room_id or "none"
    plain = f"Handle: {handle}\nDID: {account.did}\nProfile Room: {room}\nProfile: {_profile_url(handle)}"
    room_html = _room_link_html(account.profile_room_id) if account.profile_room_id else "none"
    html_message = (
        f"Handle: <b>{html.escape(handle)}</b><br>DID: <code>{html.escape(account.did)}</code><br>"
        f"Profile Room: {room_html}<br>Profile: {html.escape(_profile_url(handle))}"
    )
    await notice(request, room_id, plain, html_message=html_message)


async def _handle_create_profile(request: Request, *, sender: str, room_id: str) -> None:
    """Give the sender an ATProto account (handle, DID, repository) and a
    Profile Room: a room the bot creates and administers, where what the
    owner posts is published and whose name, topic and avatar are their
    display name, bio and avatar.

    Run again by someone who already has both, it re-invites them to the
    room -- the way back in for someone who left it.
    """
    app = request.app
    config = app.state.config
    synapse = app.state.synapse
    pds = app.state.pds

    try:
        account = await pds.create_account(sender)
    except HandleRejected as exc:
        await notice(request, room_id, f"Can't create your Bluesky identity: {exc.rejection.message}")
        return
    except PdsError as exc:
        logger.warning("create profile for %s failed: %s", sender, exc.message)
        await notice(request, room_id, "Couldn't register your identity just now -- please try again in a minute.")
        return
    handle = pds.handle_of(account)

    if account.profile_room_id:
        # Also the way back in for someone who left their rooms, and how
        # an account from before these rooms existed gets them.
        await ensure_user_rooms(app, account)
        await notice(
            request,
            room_id,
            f"You're already set up as {handle}. Your Profile Room is {account.profile_room_id}.",
            html_message=f"You're already set up as <b>{html.escape(handle)}</b>. "
            f"Your Profile Room is {_room_link_html(account.profile_room_id)}.",
        )
        return

    try:
        profile = await synapse.get_profile(sender)
    except SynapseError:
        profile = {}
    try:
        account = await create_profile_room(
            app,
            account,
            name=profile.get("displayname") or sender[1:].split(":", 1)[0],
            avatar_mxc=profile.get("avatar_url"),
        )
    except SynapseError as exc:
        logger.warning("Could not create Profile Room for %s: %s", sender, exc)
        await notice(
            request,
            room_id,
            f"Your identity {handle} exists, but I couldn't create your Profile Room. "
            f'Run "{config.appservice.command_prefix}create profile" again to retry.',
        )
        return
    new_room_id = account.profile_room_id or ""
    try:
        await ensure_declaration(app, account)
    except PdsError:
        logger.warning("Could not publish chat declaration for %s", account.did, exc_info=True)
    # Their space and notifications room. The Profile Room's own welcome
    # message is sent when they join it (see bridge.membership).
    await ensure_user_rooms(app, account)

    explanation = (
        "What you post there -- text, images, video, files -- is published on Bluesky; the room's name, topic "
        "and avatar are your display name, bio and avatar."
    )
    await notice(
        request,
        room_id,
        f"Created your Bluesky identity {handle} and invited you to its Profile Room {new_room_id}. "
        f"{explanation} Your profile: {_profile_url(handle)}",
        html_message=f"Created your Bluesky identity <b>{html.escape(handle)}</b> and invited you to its Profile "
        f"Room {_room_link_html(new_room_id)}. {explanation}<br>Your profile: {html.escape(_profile_url(handle))}",
    )


async def _handle_banner(request: Request, *, sender: str, room_id: str, argument: str, content: dict) -> None:
    """Set (or clear) the banner of the sender's profile. The banner lives
    in the Profile Room as MSC4221 room-banner state, which the profile
    record is then synced from like the avatar."""
    app = request.app
    synapse = app.state.synapse
    config = app.state.config
    bot = bot_mxid(config)
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None or not account.profile_room_id:
        return
    argument = argument.strip()
    if argument.lower() == "remove":
        mxc = ""
    elif argument.startswith("mxc://"):
        mxc = argument.split()[0]
    else:
        mxc = ""
        target = reply_target_event_id(content)
        if target and not argument:
            try:
                replied = await synapse.get_event(room_id, target, as_user_id=bot)
            except SynapseError:
                replied = None
            url = ((replied or {}).get("content") or {}).get("url")
            mxc = url if isinstance(url, str) and url.startswith("mxc://") else ""
        if not mxc:
            p = config.appservice.command_prefix
            await notice(
                request, room_id, f'Usage: {p}banner mxc://server/mediaid, reply to an image with {p}banner, or {p}banner remove.'
            )
            return
    try:
        await synapse.send_state_event(
            account.profile_room_id, PROFILE_BANNER_STATE_TYPE, {"url": mxc} if mxc else {}, as_user_id=bot
        )
        await sync_profile_record(app, account)
    except (SynapseError, PdsError):
        logger.warning("Could not set banner for %s", account.did, exc_info=True)
        await notice(request, room_id, "Couldn't update your banner.")
        return
    await notice(request, room_id, "Banner updated." if mxc else "Banner removed.")


# -- general ------------------------------------------------------------------


async def _handle_follow(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    app = request.app
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    identity = await _resolve_target(request, room_id=room_id, argument=argument, usage="follow handle.example.com")
    if identity is None:
        return
    if await _is_local(request, identity):
        await notice(request, room_id, f"@{identity.handle} is an account on this server; following those isn't supported yet.")
        return
    try:
        remote, is_new = await follow(app, account, identity)
    except (SynapseError, PdsError):
        logger.warning("Follow of %s by %s failed", identity.did, account.did, exc_info=True)
        await notice(request, room_id, f"Couldn't follow @{identity.handle} just now.")
        return
    verb = "Now following" if is_new else "You already follow"
    await notice(
        request,
        room_id,
        f"{verb} @{remote.handle}. Their posts appear in {remote.room_id}.",
        html_message=f"{verb} <b>@{html.escape(remote.handle)}</b>. Their posts appear in {_room_link_html(remote.room_id)}.",
    )


async def _handle_unfollow(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    app = request.app
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    identity = await _resolve_target(request, room_id=room_id, argument=argument, usage="unfollow handle.example.com")
    if identity is None:
        return
    if not await unfollow(app, account, identity.did):
        await notice(request, room_id, f"You don't follow @{identity.handle}.")
        return
    await notice(request, room_id, f"Unfollowed @{identity.handle}.")
    remote = await app.state.repository.get_remote_account(identity.did)
    if remote is not None:
        try:
            await app.state.synapse.kick_user(
                remote.room_id, sender, as_user_id=bot_mxid(app.state.config), reason="Unfollowed"
            )
        except SynapseError:
            pass  # not in the room


async def _handle_following(request: Request, *, sender: str, room_id: str) -> None:
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    follows = await request.app.state.repository.list_follows(account.did)
    if not follows:
        await notice(request, room_id, "You don't follow anyone yet.")
        return
    plain = "You follow:\n" + "\n".join(f"@{remote.handle} -- {remote.room_id}" for remote in follows)
    items = "".join(
        f"<li><b>@{html.escape(remote.handle)}</b> -- {_room_link_html(remote.room_id)}</li>" for remote in follows
    )
    await notice(request, room_id, plain, html_message=f"You follow:<ul>{items}</ul>")


async def _handle_import(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Mirror one post by URL, creating its author's room if needed and
    inviting the sender to it -- without following anyone."""
    app = request.app
    config = app.state.config
    reference = argument.split()[0]
    match = _POST_URL_RE.match(reference) or _AT_POST_URI_RE.match(reference)
    if match is None or parse_account_reference(match.group(1)) is None:
        await notice(
            request, room_id, f"Usage: {config.appservice.command_prefix}import https://bsky.app/profile/<handle>/post/<id>"
        )
        return
    try:
        identity = await resolve_account(
            app.state.http_client, match.group(1), plc_directory=config.atproto.plc_directory
        )
    except IdentityError as exc:
        await notice(request, room_id, str(exc))
        return
    if await _is_local(request, identity):
        await notice(request, room_id, "That post is from an account on this server.")
        return
    rkey = match.group(2)
    response = await get_record(app.state.http_client, identity.pds, identity.did, POST_COLLECTION, rkey)
    if not response or not isinstance(response.get("value"), dict):
        await notice(request, room_id, "Couldn't find that post.")
        return
    try:
        remote, _created = await ensure_remote_account(app, identity)
        await mirror_post(app, remote, rkey, response["value"], cid=str(response.get("cid", "")))
    except SynapseError:
        logger.warning("Import of %s/%s failed", identity.did, rkey, exc_info=True)
        await notice(request, room_id, "Couldn't import that post.")
        return
    await invite_to_room(app, remote.room_id, sender)
    await add_room_to_space(app, sender, remote.room_id)
    await notice(
        request,
        room_id,
        f"Imported into {remote.room_id}.",
        html_message=f"Imported into {_room_link_html(remote.room_id)}.",
    )


# Most follows one import reads from another account.
_FOLLOWS_IMPORT_MAX = 5000


def parse_follows_list(text: str) -> list[str]:
    """The accounts in an uploaded follows list: one handle, DID or
    bsky.app profile link per line, or a CSV whose first column is one
    (a header row is skipped, as it isn't any of those). Duplicates and
    lines that aren't an account are dropped."""
    references: list[str] = []
    for line in text.splitlines():
        first_column = line.split(",")[0].strip().strip('"').strip()
        reference = parse_account_reference(first_column) if first_column else None
        if reference is not None and reference not in references:
            references.append(reference)
    return references


async def follows_of(request: Request, identity: RemoteIdentity) -> list[str]:
    """The DIDs ``identity`` follows, read from its own repository."""
    records = await list_all_records(
        request.app.state.http_client, identity.pds, identity.did, FOLLOW_COLLECTION, max_records=_FOLLOWS_IMPORT_MAX
    )
    subjects = [str((record.get("value") or {}).get("subject") or "") for record in records]
    return list(dict.fromkeys(subject for subject in subjects if parse_account_reference(subject)))


def _import_follows_usage(config: Any) -> str:
    p = config.appservice.command_prefix
    return (
        f'To import follows: "{p}import follows handle.example.com" follows everyone that account follows (your old '
        f'account, say). Or upload a file listing handles or DIDs, one per line, and reply to it with "{p}import follows".'
    )


async def _handle_import_follows(request: Request, *, sender: str, room_id: str, argument: str, content: dict) -> None:
    """Follow, one after another, every account another account follows or
    an uploaded file lists. Checked here so the sender hears straight away
    if something is wrong; the following itself runs in the background
    (``run_follows_import``), as each new account gets a room and its
    recent posts -- minutes of work for a long list, far past how long a
    homeserver waits on one transaction."""
    app = request.app
    config = app.state.config
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    if argument.strip():
        try:
            source = await resolve_account(
                app.state.http_client, argument.split()[0], plc_directory=config.atproto.plc_directory
            )
        except IdentityError as exc:
            await notice(request, room_id, str(exc))
            return
        references = await follows_of(request, source)
        if not references:
            await notice(request, room_id, f"@{source.handle} doesn't follow anyone, or I couldn't read who they follow.")
            return
    else:
        target = reply_target_event_id(content)
        if target is None:
            await notice(request, room_id, _import_follows_usage(config))
            return
        try:
            replied = await app.state.synapse.get_event(room_id, target, as_user_id=bot_mxid(config))
        except SynapseError:
            replied = None
        mxc = ((replied or {}).get("content") or {}).get("url")
        if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
            await notice(request, room_id, f"The message you replied to has no file attached. {_import_follows_usage(config)}")
            return
        try:
            download = await app.state.synapse.download_media(mxc)
        except SynapseError:
            download = None
        if download is None:
            await notice(request, room_id, "Couldn't download that file -- try uploading it again.")
            return
        references = parse_follows_list(download.content.decode("utf-8", errors="replace"))
        if not references:
            await notice(
                request, room_id,
                "That file doesn't list any Bluesky accounts -- expected one handle (alice.bsky.social), DID or "
                "bsky.app profile link per line.",
            )
            return
    await notice(
        request, room_id,
        f"Importing {len(references)} follows -- this can take a while (each new account gets its own room). "
        "I'll post a summary here when it's done.",
    )
    run_in_background(app, run_follows_import(request, account=account, room_id=room_id, references=references))


async def run_follows_import(request: Request, *, account: Account, room_id: str, references: list[str]) -> None:
    """Follow each account in turn, then post one summary. Accounts already
    followed (and the importer's own, which a copy of their old account's
    follows can contain) are only counted; each failure is listed with its
    reason."""
    app = request.app
    repository = app.state.repository
    plc = app.state.config.atproto.plc_directory
    own = (account.did, app.state.pds.handle_of(account))
    followed = skipped = 0
    failures: list[tuple[str, str]] = []
    try:
        for reference in references:
            if reference in own:
                skipped += 1
                continue
            try:
                identity = await resolve_account(app.state.http_client, reference, plc_directory=plc)
            except IdentityError as exc:
                failures.append((reference, str(exc)))
                continue
            if identity.did == account.did or await repository.get_follow_rkey(account.did, identity.did) is not None:
                skipped += 1
                continue
            if await _is_local(request, identity):
                failures.append((f"@{identity.handle}", "an account on this server; following those isn't supported yet"))
                continue
            try:
                await follow(app, account, identity, backfill_in_background=False)
            except (SynapseError, PdsError):
                logger.warning("Import follow of %s by %s failed", identity.did, account.did, exc_info=True)
                failures.append((f"@{identity.handle}", "couldn't follow just now"))
                continue
            followed += 1
    except Exception:
        logger.exception("Follows import for %s stopped early", account.did)
        await notice(
            request, room_id, "The follows import hit an unexpected error and stopped early -- the summary below covers what was done."
        )
    summary = f"Follows import finished: {followed} followed, {skipped} already followed (skipped), {len(failures)} failed."
    if not failures:
        await notice(request, room_id, summary)
        return
    plain = summary + "\n" + "\n".join(f"  - {who}: {reason}" for who, reason in failures)
    items = "".join(f"<li><code>{html.escape(who)}</code> -- {html.escape(reason)}</li>" for who, reason in failures)
    await notice(request, room_id, plain, html_message=f"<p>{html.escape(summary)}</p><ul>{items}</ul>")


async def _handle_widget(request: Request, *, sender: str, room_id: str) -> None:
    """Add the bridge's widget to this room (see ``bridge.room_widget``)."""
    if await add_bridge_widget(request.app, room_id):
        await notice(request, room_id, "Added the Bluesky Bridge widget to this room -- open it from your client's widgets/apps panel.")
    else:
        await notice(request, room_id, "Couldn't add the widget here -- I need permission to change this room's settings.")


async def _handle_repost(request: Request, *, sender: str, room_id: str, event: dict, argument: str) -> None:
    """``repost`` as a reply to a post reposts it; with text after the
    command word, it quote-posts it with that text instead."""
    # Imported here: bridge.interactions itself builds on this module.
    from bridge.interactions import send_quote, send_repost
    from bridge.matrix_records import message_body
    from bridge.post_lookup import locate_post_by_event

    app = request.app
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    content = event.get("content") or {}
    target_event = reply_target_event_id(content)
    target = await locate_post_by_event(app, room_id, target_event) if target_event else None
    p = app.state.config.appservice.command_prefix
    if target is None:
        await notice(
            request, room_id, f"Reply to a post with {p}repost to repost it, or {p}repost <your text> to quote it."
        )
        return
    try:
        if argument.strip():
            # Where the caption starts within the text the sender wrote.
            body = message_body(content)
            offset = len(body) - len(body.lstrip()) + _first_line(content).lower().index("repost") + len("repost")
            if await send_quote(request, account, target, event, offset=offset):
                await notice(request, room_id, "Quoted.")
        elif await send_repost(request, account, target, source_event_id=event["event_id"]):
            await notice(request, room_id, "Reposted.")
        else:
            await notice(request, room_id, "You've already reposted that.")
    except PdsError:
        logger.warning("Repost by %s failed", account.did, exc_info=True)
        await notice(request, room_id, "Couldn't repost that.")


# -- advanced / maintenance ---------------------------------------------------


async def _block_rkey(request: Request, account: Account, did: str) -> str | None:
    """The record key of ``account``'s block of ``did``, if there is one.
    Block records are stored inline, so their subject can be read back."""
    for row in await request.app.state.repository.list_records(account.did):
        if row.collection == BLOCK_COLLECTION and json.loads(row.source).get("record", {}).get("subject") == did:
            return row.rkey
    return None


async def _handle_block(request: Request, *, sender: str, room_id: str, argument: str, block: bool) -> None:
    app = request.app
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    word = "block" if block else "unblock"
    identity = await _resolve_target(request, room_id=room_id, argument=argument, usage=f"{word} handle.example.com")
    if identity is None:
        return
    pds = app.state.pds
    existing = await _block_rkey(request, account, identity.did)
    try:
        if block and existing is None:
            record = {"$type": BLOCK_COLLECTION, "subject": identity.did, "createdAt": now_timestamp()}
            await pds.apply_writes(account.did, [Write("create", BLOCK_COLLECTION, pds.next_record_key(), record)])
            await unfollow(app, account, identity.did)
        elif not block and existing is not None:
            await pds.apply_writes(account.did, [Write("delete", BLOCK_COLLECTION, existing)])
        else:
            state = "already blocked" if block else "not blocked"
            await notice(request, room_id, f"@{identity.handle} is {state}.")
            return
    except PdsError:
        logger.warning("%s of %s by %s failed", word, identity.did, account.did, exc_info=True)
        await notice(request, room_id, f"Couldn't {word} @{identity.handle}.")
        return
    await notice(request, room_id, f"{'Blocked' if block else 'Unblocked'} @{identity.handle}.")


async def _remote_for_room(request: Request, room_id: str, *, command: str) -> RemoteAccount | None:
    remote = await request.app.state.repository.get_remote_account_for_room(room_id)
    if remote is None:
        p = request.app.state.config.appservice.command_prefix
        await notice(request, room_id, f"Run {p}{command} inside a Bluesky account's room.")
    return remote


async def _handle_backfill(request: Request, *, sender: str, room_id: str, argument: str, content: dict) -> None:
    """Pull an account's latest posts into the Remote User Room this is
    run in -- or, run as a reply to a mirrored post (or inside its
    thread), that post's replies instead.

    Everyone gets the configured default count. Only a server admin may
    give an explicit ``N``: an arbitrarily large backfill is an unbounded
    number of fetches (and media downloads) against someone else's server
    that any local user could otherwise trigger at will.
    """
    app = request.app
    remote = await _remote_for_room(request, room_id, command="backfill")
    if remote is None:
        return
    count = app.state.config.atproto.follow_backfill_count
    if argument:
        if not await is_admin(app, sender):
            await notice(request, room_id, "Only a server admin can choose a custom backfill count.")
            return
        if not argument.isdigit():
            await notice(request, room_id, f'"{argument}" isn\'t a number.')
            return
        count = int(argument)
        if count <= 0:
            await notice(request, room_id, "Give a positive number of posts to backfill.")
            return

    relates_to = content.get("m.relates_to") or {}
    root_event_id = relates_to.get("event_id") if relates_to.get("rel_type") == "m.thread" else reply_target_event_id(content)
    root = await app.state.repository.get_mirrored_post_for_event(root_event_id) if root_event_id else None
    if root_event_id and root is None:
        await notice(request, room_id, "That isn't a mirrored post, so there's no conversation to backfill.")
        return
    what = "replies" if root is not None else "posts"
    await notice(
        request, room_id, f"Backfilling up to {count} {what} -- this can take a while. I'll post a summary here when it's done."
    )

    async def run() -> None:
        if root is not None:
            mirrored = await backfill_thread(app, remote, root.uri, root.event_ids[0], count)
        else:
            mirrored = await backfill(app, remote, count)
        await notice(request, room_id, f"Backfill done: {mirrored} new {what}.")

    run_in_background(app, run())


async def _handle_chat(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """``chat``/``dm``: open a Bluesky chat with an account as a Matrix
    direct-message room. Bluesky has one kind of direct message, so both
    words mean the same thing."""
    app = request.app
    account = await _require_account(request, sender=sender, room_id=room_id)
    if account is None:
        return
    identity = await _resolve_target(request, room_id=room_id, argument=argument, usage="chat handle.example.com")
    if identity is None:
        return
    if await _is_local(request, identity):
        await notice(request, room_id, f"@{identity.handle} is on this server -- just start an ordinary Matrix DM with them.")
        return
    try:
        chat_room_id = await start_chat(app, account, identity)
    except ChatError as exc:
        await notice(request, room_id, f"Couldn't start a chat with @{identity.handle}: {exc}")
        return
    except (SynapseError, PdsError):
        logger.warning("Chat with %s for %s failed", identity.did, account.did, exc_info=True)
        await notice(request, room_id, f"Couldn't create a chat room with @{identity.handle}.")
        return
    await notice(
        request,
        room_id,
        f"Your chat with @{identity.handle} is {chat_room_id}.",
        html_message=f"Your chat with <b>@{html.escape(identity.handle)}</b> is {_room_link_html(chat_room_id)}.",
    )
    try:
        encrypted = await app.state.synapse.get_room_state(
            chat_room_id, "m.room.encryption", as_user_id=bot_mxid(app.state.config)
        )
    except SynapseError:
        encrypted = None
    if encrypted:
        # The homeserver forces encryption on new private rooms; the
        # bridge has no end-to-end encryption support.
        await notice(
            request,
            room_id,
            "That room came out encrypted, which this homeserver forces on new private rooms. I can't read "
            "encrypted rooms, so messages there won't be sent. A server admin needs to change that setting.",
        )


async def _handle_refresh(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Re-read a remote account and bring everything kept in sync with it
    up to date now, as matrix-appservice-activitypub's ``;refresh`` does:
    its ghost's and room's name, avatar, bio and banner, and the room and
    ghost brought into line with the current settings either way (see
    ``sync_remote_settings``). Server admins only."""
    app = request.app
    if not await is_admin(app, sender):
        await notice(request, room_id, "Only a server admin can refresh an account.")
        return
    identity = await _resolve_target(request, room_id=room_id, argument=argument, usage="refresh handle.example.com")
    if identity is None:
        return
    remote = await app.state.repository.get_remote_account(identity.did)
    if remote is None:
        await notice(request, room_id, f"Nobody here follows @{identity.handle}, so there's nothing to refresh.")
        return
    remote = await refresh_remote_account(app, remote, identity)
    await sync_remote_settings(app, remote)
    await notice(request, room_id, f"Refreshed @{remote.handle}.")
