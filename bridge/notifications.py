"""Sends the bot's own notices about a user's account (new follower, like,
repost, reply, mention, quote) to a private room with the bot, rather than
into their Profile Room.

A Profile Room can have other Matrix users invited into it by its owner.
Notices dropped there would show every co-occupant who is interacting with
the owner -- a leak with no Bluesky-side equivalent. A room with just the
bot has no such audience.

One notifications room per Matrix user, created when they first set up
their identity so it is there waiting rather than arriving as a surprise
invite later.

Every notification is an ordinary ``m.text`` message -- never ``m.notice``
(which clients render quietly, and default push rules never notify for)
and never tagging the recipient. Whether this room notifies is then
entirely the recipient's own per-room setting, which is the reason it is a
room of its own.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI

from bridge.room_widget import add_bridge_widget
from bridge.spaces import NOTIFICATIONS_ROOM_SPACE_ORDER, add_room_to_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

NOTIFIED_EVENTS = "new followers, likes, reposts, replies, mentions and quotes"


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def ensure_notifications_room(app: FastAPI, matrix_user_id: str) -> str | None:
    """Get-or-create the user's notifications room, inviting them into it
    and adding it to their space when it is created. Returns the room ID,
    or ``None`` if creation failed."""
    repository = app.state.repository
    existing = await repository.get_notifications_room(matrix_user_id)
    if existing is not None:
        return existing
    config = app.state.config
    try:
        room_id = await app.state.synapse.create_room(
            as_user_id=_bot(app),
            name=config.bridge.notifications_room_name,
            invite=[matrix_user_id],
            is_direct=True,
            preset="trusted_private_chat",
            avatar_mxc=config.bridge.notifications_room_avatar_mxc or config.appservice.bot_avatar_mxc,
        )
    except SynapseError:
        logger.warning("Could not create notifications room for %s", matrix_user_id, exc_info=True)
        return None
    await repository.set_notifications_room(matrix_user_id, room_id)
    await add_bridge_widget(app, room_id)
    await add_room_to_space(app, matrix_user_id, room_id, order=NOTIFICATIONS_ROOM_SPACE_ORDER)
    name = config.bridge.notifications_room_name
    explanation = (
        f"I'll message you here whenever something happens on Bluesky that involves you -- {NOTIFIED_EVENTS}. "
        "Stay in this room to keep receiving them."
    )
    # An ordinary message with a bold header: the first thing in a brand
    # new room should stand out, not be muted like a notice.
    await notify_user(
        app,
        matrix_user_id,
        {
            "msgtype": "m.text",
            "body": f"{name}\n{explanation}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p><strong>{html.escape(name)}</strong></p><p>{html.escape(explanation)}</p>",
        },
    )
    logger.info("Created notifications room %s for %s", room_id, matrix_user_id)
    return room_id


async def notify_user(app: FastAPI, matrix_user_id: str, content: dict[str, Any]) -> str | None:
    """Send ``content`` (a full message-event content dict) to the user's
    notifications room, creating it first if needed. Returns the event
    ID, or ``None`` if it couldn't be sent -- best-effort, never meant to
    block whatever triggered the notification."""
    room_id = await ensure_notifications_room(app, matrix_user_id)
    if room_id is None:
        return None
    try:
        return await app.state.synapse.send_message_event(room_id, content, as_user_id=_bot(app))
    except SynapseError:
        logger.warning("Could not notify %s in %s", matrix_user_id, room_id, exc_info=True)
        return None
