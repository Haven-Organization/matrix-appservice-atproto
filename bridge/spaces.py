"""Per-Matrix-user space (``m.space``) that organizes every room this
bridge manages for them: their Profile Room, their notifications room, and
every Remote User Room they're in (accounts they follow, or whose posts
they imported).

One space per Matrix user, created the first time they end up in any
bridge-managed room. The bot is its creator and the user sits one level
below (99), the same as in a Profile Room, so the bot can always re-invite
them.

Membership is kept in sync reactively from ``bridge.membership``: joining
a bridge-managed room adds it to the space; leaving a Remote User Room
removes it again. A Profile Room or notifications room is never removed.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

_SPACE_ROOM_TYPE = "m.space"

# m.space.child's `order`: a child with one sorts before every child
# without. These keep the Profile Room and notifications room pinned, in
# that order, above the unordered rooms of followed accounts.
PROFILE_ROOM_SPACE_ORDER = "0"
NOTIFICATIONS_ROOM_SPACE_ORDER = "1"


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def ensure_user_space(app: FastAPI, matrix_user_id: str) -> str | None:
    """Get-or-create ``matrix_user_id``'s space. Returns its room ID, or
    ``None`` if it couldn't be created -- best-effort, like the rest of
    the bridge's room bookkeeping."""
    repository = app.state.repository
    existing = await repository.get_user_space(matrix_user_id)
    if existing is not None:
        return existing
    config = app.state.config
    try:
        space_room_id = await app.state.synapse.create_room(
            as_user_id=_bot(app),
            name=config.bridge.space_name,
            invite=[matrix_user_id],
            avatar_mxc=config.bridge.space_avatar_mxc or config.appservice.bot_avatar_mxc,
            room_type=_SPACE_ROOM_TYPE,
            power_level_users={matrix_user_id: 99},
        )
    except SynapseError:
        logger.warning("Could not create space for %s", matrix_user_id, exc_info=True)
        return None
    await repository.set_user_space(matrix_user_id, space_room_id)
    await add_bridge_widget(app, space_room_id)
    logger.info("Created space %s for %s", space_room_id, matrix_user_id)
    return space_room_id


async def _set_space_child(
    app: FastAPI, *, space_room_id: str, child_room_id: str, add: bool, order: str | None = None
) -> None:
    """Add or remove a space child. Per the spec an ``m.space.child`` with
    no ``via`` means "not a child", so removal is overwriting it with
    empty content."""
    synapse = app.state.synapse
    bot = _bot(app)
    via = [app.state.config.synapse.server_name]
    content: dict = {}
    if add:
        content = {"via": via}
        if order is not None:
            content["order"] = order
    try:
        await synapse.send_state_event(
            space_room_id, "m.space.child", content, as_user_id=bot, state_key=child_room_id
        )
    except SynapseError:
        logger.info("Could not update %s in space %s", child_room_id, space_room_id, exc_info=True)
        return
    if not add:
        return
    # The child's own pointer back to its space, which some clients use
    # for breadcrumbs. Not what makes it appear in the space.
    try:
        await synapse.send_state_event(
            child_room_id, "m.space.parent", {"via": via, "canonical": True}, as_user_id=bot, state_key=space_room_id
        )
    except SynapseError:
        logger.info("Could not set m.space.parent on %s", child_room_id, exc_info=True)


async def add_room_to_space(
    app: FastAPI, matrix_user_id: str, child_room_id: str, *, order: str | None = None
) -> None:
    """Add ``child_room_id`` to ``matrix_user_id``'s space, creating the
    space first if needed. Idempotent."""
    space_room_id = await ensure_user_space(app, matrix_user_id)
    if space_room_id is not None:
        await _set_space_child(app, space_room_id=space_room_id, child_room_id=child_room_id, add=True, order=order)


async def remove_room_from_space(app: FastAPI, matrix_user_id: str, child_room_id: str) -> None:
    """Remove ``child_room_id`` from ``matrix_user_id``'s space. A no-op
    if they have no space."""
    space_room_id = await app.state.repository.get_user_space(matrix_user_id)
    if space_room_id is not None:
        await _set_space_child(app, space_room_id=space_room_id, child_room_id=child_room_id, add=False)
