"""Bridge-info state events: marking a room as bridged, and to what.

Every Profile Room and Remote User Room carries a state event naming the
bridge, the protocol and the specific ATProto account the room represents,
so bridge-aware clients can show that provenance instead of the room
looking like an ordinary one. Same shape as matrix-appservice-activitypub's.

Sent under both the current ``m.bridge`` type (MSC2346) and the legacy
``uk.half-shot.bridge`` type it was renamed from: Element Web's room
settings "Bridges" tab still only reads the legacy one.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

_EVENT_TYPES = ("m.bridge", "uk.half-shot.bridge")
PROTOCOL_ID = "atproto"
PROTOCOL_DISPLAY_NAME = "AT Protocol"


async def send_bridge_info(
    app: FastAPI, *, room_id: str, did: str, display_name: str | None, avatar_mxc: str | None
) -> None:
    """Tag ``room_id`` as bridged to the account ``did``. Best-effort: the
    room is fully usable without it. Safe to repeat -- the homeserver
    doesn't create a new event for unchanged state."""
    config = app.state.config
    bot = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    channel: dict = {"id": did}
    if display_name:
        channel["displayname"] = display_name
    if avatar_mxc:
        channel["avatar_url"] = avatar_mxc
    protocol: dict = {"id": PROTOCOL_ID, "displayname": PROTOCOL_DISPLAY_NAME}
    if config.appservice.bot_avatar_mxc:
        # The bot's own avatar doubles as the protocol's icon.
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    content = {"bridgebot": bot, "creator": bot, "protocol": protocol, "channel": channel}
    for event_type in _EVENT_TYPES:
        try:
            await app.state.synapse.send_state_event(
                room_id, event_type, content, as_user_id=bot, state_key=f"{PROTOCOL_ID}://{did}"
            )
        except SynapseError:
            logger.info("Could not set %s state event in %s", event_type, room_id, exc_info=True)


async def refresh_all_bridge_info(app: FastAPI) -> None:
    """Make sure every existing Profile Room and Remote User Room carries
    current bridge info -- run at startup, so rooms made before this
    existed (or before the bot's avatar was configured) get it too."""
    repository = app.state.repository
    synapse = app.state.synapse
    config = app.state.config
    bot = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    async def room_details(room_id: str) -> tuple[str | None, str | None]:
        try:
            name = await synapse.get_room_state(room_id, "m.room.name", as_user_id=bot)
            avatar = await synapse.get_room_state(room_id, "m.room.avatar", as_user_id=bot)
        except SynapseError:
            return None, None
        return (name or {}).get("name"), (avatar or {}).get("url")

    rooms = [(a.profile_room_id, a.did) for a in await repository.list_accounts() if a.profile_room_id]
    rooms += [(r.room_id, r.did) for r in await repository.list_remote_accounts()]
    for room_id, did in rooms:
        name, avatar = await room_details(room_id)
        await send_bridge_info(app, room_id=room_id, did=did, display_name=name, avatar_mxc=avatar)
