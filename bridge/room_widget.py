"""Adds the bridge's room widget (the web UI served by ``bridge.widget``) to
a room as a state event, as matrix-appservice-activitypub does: on the
``widget`` command, and whenever the bridge creates a room.

Each room is given the widget once. ``add_bridge_widget`` records that it
did (``widget_added:<room>``) so the startup pass (``add_missing_widgets``)
can give it to rooms made before the widget existed without putting it
back into a room whose members removed it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI

from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# Sent under both the current ``m.widget`` type and the legacy
# ``im.vector.modular.widgets`` type it superseded: clients vary in which
# they look for (the same reasoning as the dual m.bridge/uk.half-shot.bridge
# bridge info).
WIDGET_STATE_TYPES = ("m.widget", "im.vector.modular.widgets")
WIDGET_NAME = "Bluesky Bridge"


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def _mark_key(room_id: str) -> str:
    return f"widget_added:{room_id}"


async def add_bridge_widget(app: FastAPI, room_id: str) -> bool:
    """Add the widget to ``room_id`` as the bot. A fresh widget ID every
    call, so running it in a room that already has one adds a second
    rather than doing nothing. Returns whether it worked; never raises."""
    config = app.state.config
    synapse = app.state.synapse
    bot = _bot(app)
    widget_id = f"atproto-bridge-{uuid.uuid4().hex[:12]}"
    content = {
        "type": "m.custom",
        "url": (
            f"{config.bridge.public_base_url}/widget"
            f"?matrix_user_id=$matrix_user_id&matrix_room_id=$matrix_room_id&theme=$theme&widgetId={widget_id}"
        ),
        "name": WIDGET_NAME,
        "data": {"title": WIDGET_NAME},
    }
    if config.appservice.bot_avatar_mxc:
        # What Element shows as the widget's icon in its widget list.
        content["avatar_url"] = config.appservice.bot_avatar_mxc
    ok = True
    for state_type in WIDGET_STATE_TYPES:
        try:
            await synapse.send_state_event(room_id, state_type, content, state_key=widget_id, as_user_id=bot)
        except SynapseError as exc:
            ok = False
            logger.warning("Could not add widget (%s) to %s: %s", state_type, room_id, exc)
    if ok:
        await app.state.repository.set_value(_mark_key(room_id), widget_id)
    return ok


async def _managed_rooms(app: FastAPI) -> list[str]:
    """Every room the bridge made: Remote User Rooms, and each local user's
    Profile Room, space, notifications room and chat rooms."""
    repository = app.state.repository
    rooms = [remote.room_id for remote in await repository.list_remote_accounts()]
    for account in await repository.list_accounts():
        if account.profile_room_id:
            rooms.append(account.profile_room_id)
        for extra in (
            await repository.get_user_space(account.matrix_user_id),
            await repository.get_notifications_room(account.matrix_user_id),
        ):
            if extra:
                rooms.append(extra)
        rooms += [chat.room_id for chat in await repository.list_chat_rooms(account.did)]
    return list(dict.fromkeys(rooms))


async def add_missing_widgets(app: FastAPI) -> None:
    """At startup: give the widget to every room the bridge made that has
    never had it."""
    repository = app.state.repository
    added = 0
    for room_id in await _managed_rooms(app):
        if await repository.get_value(_mark_key(room_id)) is None and await add_bridge_widget(app, room_id):
            added += 1
    if added:
        logger.info("Added the room widget to %d existing room(s)", added)
