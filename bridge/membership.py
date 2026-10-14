"""Reacting to people joining and leaving the rooms the bridge manages.

Joining keeps each user's space (``bridge.spaces``) in step with the rooms
they're actually in, and is when the orientation message for a new Profile
Room is sent. Leaving a Remote User Room unfollows that account and takes
the room back out of the space.
"""

from __future__ import annotations

import html
import logging

from fastapi import Request

from bridge.commands import bot_mxid, is_bridge_user
from bridge.follows import unfollow
from bridge.notifications import NOTIFIED_EVENTS
from bridge.spaces import (
    NOTIFICATIONS_ROOM_SPACE_ORDER,
    PROFILE_ROOM_SPACE_ORDER,
    add_room_to_space,
    remove_room_from_space,
)
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


async def _welcome_to_profile_room(request: Request, *, room_id: str, joined_user: str) -> None:
    """Sent as an ordinary ``m.text``, not ``m.notice``: someone's first
    message in their brand new room should stand out, not be muted. A bold
    header and a real HTML list, with a ``-``-prefixed plain fallback."""
    config = request.app.state.config
    p = config.appservice.command_prefix
    space_name = config.bridge.space_name
    notifications_name = config.bridge.notifications_room_name
    space_room_id = await request.app.state.repository.get_user_space(joined_user)

    # Nested under one bullet: these are all facets of the same idea
    # ("this room IS your profile"), not separate tips.
    profile_points = [
        "Anything you post here (that isn't a bot command) goes out as a post -- text, images, video and files.",
        "This room's name is your display name.",
        "This room's avatar is your avatar.",
        "This room's topic is your bio.",
        f'Use "{p}banner mxc://server/mediaid" to set your profile\'s banner image.',
    ]
    profile_points_html = [html.escape(point) for point in profile_points[:-1]] + [
        f"Use <code>{html.escape(p)}banner mxc://server/mediaid</code> to set your profile's banner image."
    ]
    notifications_point = (
        f'I\'ve also invited you to "{notifications_name}" -- accept it to get notified there of '
        f"{NOTIFIED_EVENTS} (kept separate from this room so anyone else you've invited in here can't see them)."
    )
    plain_bullets = [
        "This room IS your Bluesky profile:\n" + "\n".join(f"  - {point}" for point in profile_points),
        f'Use "{p}follow handle.example.com" to start following Bluesky accounts.',
        notifications_point,
    ]
    html_bullets = [
        "This room IS your Bluesky profile:<ul>" + "".join(f"<li>{point}</li>" for point in profile_points_html) + "</ul>",
        f"Use <code>{html.escape(p)}follow handle.example.com</code> to start following Bluesky accounts.",
        html.escape(notifications_point),
    ]
    if space_room_id:
        link = f"https://matrix.to/#/{space_room_id}?via={config.synapse.server_name}"
        plain_bullets.append(
            f"Your {space_name} space ({link}) keeps this room and every account you follow together automatically."
        )
        # A matrix.to room link renders as a pill showing the room's OWN
        # name, so "space" has to sit outside the tag or it is swallowed.
        html_bullets.append(
            f'Your <a href="{html.escape(link, quote=True)}">{html.escape(space_name)}</a> space keeps this room '
            "and every account you follow together automatically."
        )
    plain_bullets.append(f'Use "{p}help" to see everything else I can do.')
    html_bullets.append(f"Use <code>{html.escape(p)}help</code> to see everything else I can do.")

    content = {
        "msgtype": "m.text",
        "body": "Welcome to Bluesky!\n" + "\n".join(f"- {bullet}" for bullet in plain_bullets),
        "format": "org.matrix.custom.html",
        "formatted_body": "<p><strong>Welcome to Bluesky!</strong></p><ul>"
        + "".join(f"<li>{bullet}</li>" for bullet in html_bullets)
        + "</ul>",
    }
    try:
        await request.app.state.synapse.send_message_event(room_id, content, as_user_id=bot_mxid(config))
    except SynapseError:
        logger.info("Could not send welcome message to %s", room_id, exc_info=True)


async def maybe_handle_knock(request: Request, event: dict) -> bool:
    """Returns True if this was someone knocking on a room the bridge
    manages, as in matrix-appservice-activitypub: a knock on any Remote
    User Room is let in (invited by the room's ghost), and a knock on a
    local user's Profile Room only if it is that user's own. Letting
    someone in never follows the account for them -- only ``follow`` does
    that. Knocks from other homeservers are left for someone inside to
    answer, unless ``bridge.accept_federated_knocks`` is on."""
    if event.get("type") != "m.room.member" or (event.get("content") or {}).get("membership") != "knock":
        return False
    room_id = event.get("room_id", "")
    knocker = event.get("state_key", "")
    config = request.app.state.config
    if not room_id or not knocker or is_bridge_user(config, knocker):
        return False
    if knocker.split(":", 1)[-1] != config.synapse.server_name and not config.bridge.accept_federated_knocks:
        return False
    repository = request.app.state.repository
    remote = await repository.get_remote_account_for_room(room_id)
    if remote is not None:
        inviter = remote.ghost_user_id
    else:
        owner = await repository.get_account_for_profile_room(room_id)
        if owner is None or owner.matrix_user_id != knocker:
            return False
        inviter = bot_mxid(config)
    try:
        await request.app.state.synapse.invite_user(room_id, knocker, as_user_id=inviter)
    except SynapseError:
        logger.warning("Could not let %s into %s", knocker, room_id, exc_info=True)
    return True


async def maybe_handle_join(request: Request, event: dict) -> bool:
    """Returns True if this was a local user joining a room the bridge
    manages for them -- their Profile Room, their notifications room, or a
    Remote User Room -- handled by adding it to their space (created first
    if this is their first such room), plus the welcome message for their
    own Profile Room."""
    if event.get("type") != "m.room.member" or (event.get("content") or {}).get("membership") != "join":
        return False
    if ((event.get("unsigned") or {}).get("prev_content") or {}).get("membership") == "join":
        return False  # a profile change, not a join
    room_id = event.get("room_id", "")
    user = event.get("state_key", "")
    config = request.app.state.config
    if not room_id or not user or is_bridge_user(config, user):
        return False
    if user.split(":", 1)[-1] != config.synapse.server_name:
        return False

    app = request.app
    repository = app.state.repository
    profile_account = await repository.get_account_for_profile_room(room_id)
    if profile_account is not None and profile_account.matrix_user_id == user:
        await add_room_to_space(app, user, room_id, order=PROFILE_ROOM_SPACE_ORDER)
        await _welcome_to_profile_room(request, room_id=room_id, joined_user=user)
        return True
    if await repository.get_notifications_room_owner(room_id) == user:
        await add_room_to_space(app, user, room_id, order=NOTIFICATIONS_ROOM_SPACE_ORDER)
        return True
    if await repository.get_remote_account_for_room(room_id) is not None:
        await add_room_to_space(app, user, room_id)
        return True
    return False


async def maybe_handle_leave(request: Request, event: dict) -> bool:
    """Returns True if a local user left (or was removed from) a Remote
    User Room -- which unfollows that account for them and drops the room
    from their space: the room is where its posts arrive, so leaving it is
    saying they're no longer wanted."""
    if event.get("type") != "m.room.member":
        return False
    if (event.get("content") or {}).get("membership") not in ("leave", "ban"):
        return False
    user = event.get("state_key", "")
    config = request.app.state.config
    if not user or is_bridge_user(config, user):
        return False
    app = request.app
    repository = app.state.repository
    remote = await repository.get_remote_account_for_room(event.get("room_id", ""))
    if remote is None:
        return False
    await remove_room_from_space(app, user, remote.room_id)
    account = await repository.get_account_for_user(user)
    if account is not None and await unfollow(app, account, remote.did):
        logger.info("%s left %s: unfollowed %s", user, remote.room_id, remote.handle)
    return True
