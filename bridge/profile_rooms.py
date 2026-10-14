"""Creating a local user's Profile Room.

Shared by ``create profile`` and ``replace room``: a room the bot creates
and administers, marked as an MSC4501 profile room owned by the user, with
the account bound to it and the profile record published from it.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.msc4501 import (
    SOCIAL_PROFILE_ROOM_TYPE,
    SOCIAL_PROFILE_USER_ID_POWER_LEVEL,
    SOCIAL_PROFILE_USER_ID_STATE_TYPE,
)
from bridge.pds import PdsError
from bridge.profile_sync import sync_profile_record
from bridge.repository import Account
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def mark_profile_owner(app: FastAPI, room_id: str, matrix_user_id: str) -> None:
    """MSC4501: record whose profile the room is. Best-effort -- in a room
    the bot didn't create it may lack the power to."""
    try:
        await app.state.synapse.send_state_event(
            room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": matrix_user_id}, as_user_id=_bot(app)
        )
    except SynapseError:
        logger.info("Could not set %s in %s", SOCIAL_PROFILE_USER_ID_STATE_TYPE, room_id, exc_info=True)


async def bind_profile_room(app: FastAPI, account: Account, room_id: str) -> Account:
    """Make ``room_id`` the account's Profile Room and publish the profile
    from it. Returns the updated account."""
    repository = app.state.repository
    await repository.set_profile_room(account.did, room_id)
    account = await repository.get_account(account.did) or account
    await mark_profile_owner(app, room_id, account.matrix_user_id)
    try:
        await sync_profile_record(app, account)
    except (SynapseError, PdsError):
        logger.warning("Could not write profile record for %s", account.did, exc_info=True)
    return account


async def create_profile_room(
    app: FastAPI,
    account: Account,
    *,
    name: str,
    avatar_mxc: str | None = None,
    topic: str | None = None,
    invite: list[str] | None = None,
    predecessor: dict[str, str] | None = None,
) -> Account:
    """Create a Profile Room for ``account`` and bind it. Raises
    ``SynapseError`` if the room can't be made."""
    owner = account.matrix_user_id
    room_id = await app.state.synapse.create_room(
        as_user_id=_bot(app),
        name=name,
        topic=topic,
        invite=invite if invite is not None else [owner],
        avatar_mxc=avatar_mxc,
        room_type=SOCIAL_PROFILE_ROOM_TYPE,
        # One below the bot (the creator): the owner can change the name,
        # topic and avatar and moderate the room, while the bot can always
        # still act in it.
        power_level_users={owner: 99},
        # MSC4501's own guidance: otherwise any moderator, not just the
        # bot, could reassign whose profile this room is.
        power_level_events={SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
        predecessor=predecessor,
        join_rule=app.state.config.bridge.local_profile_room_join_rule,
    )
    logger.info("Created Profile Room %s for %s", room_id, owner)
    return await bind_profile_room(app, account, room_id)
