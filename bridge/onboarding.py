"""The rooms every user of the bridge has: a space, a notifications room,
and -- filed inside the space -- their Profile Room and the rooms of the
accounts they follow.

``ensure_user_rooms`` creates whatever is missing and re-invites the user
to whatever they're not in. It runs when an identity is created, and again
whenever ``create profile`` is repeated -- which makes that command the
way back in for someone who left any of them, and the way to give these
rooms to an account that was created before they existed.
"""

from __future__ import annotations

from fastapi import FastAPI

from bridge.follows import invite_to_room
from bridge.notifications import ensure_notifications_room
from bridge.repository import Account
from bridge.spaces import PROFILE_ROOM_SPACE_ORDER, add_room_to_space, ensure_user_space


async def ensure_user_rooms(app: FastAPI, account: Account) -> dict[str, str | None]:
    """Returns the room IDs: ``space``, ``notifications``, ``profile``."""
    user = account.matrix_user_id
    space = await ensure_user_space(app, user)
    if account.profile_room_id:
        await add_room_to_space(app, user, account.profile_room_id, order=PROFILE_ROOM_SPACE_ORDER)
    notifications = await ensure_notifications_room(app, user)
    for remote in await app.state.repository.list_follows(account.did):
        await add_room_to_space(app, user, remote.room_id)
    for room_id in (space, notifications, account.profile_room_id):
        if room_id:
            await invite_to_room(app, room_id, user)
    return {"space": space, "notifications": notifications, "profile": account.profile_room_id}
