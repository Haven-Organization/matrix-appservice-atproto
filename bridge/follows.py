"""Following and unfollowing remote accounts on a local user's behalf.

A follow is two things at once: an ``app.bsky.graph.follow`` record in the
local user's repository (what the rest of the network sees) and membership
of the remote account's Remote User Room (where its posts arrive). They
are kept together here, so that leaving the room unfollows and unfollowing
leaves the room.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from bridge.atproto.identity import RemoteIdentity
from bridge.pds import PdsError, Write
from bridge.remote_accounts import backfill, ensure_remote_account
from bridge.repository import Account, RemoteAccount
from bridge.spaces import add_room_to_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

FOLLOW_COLLECTION = "app.bsky.graph.follow"


def now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_in_background(app: FastAPI, coroutine) -> None:
    """Run ``coroutine`` without making the caller wait, keeping a
    reference so it isn't garbage-collected mid-flight."""
    task = asyncio.create_task(coroutine)
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def invite_to_room(app: FastAPI, room_id: str, user_id: str) -> None:
    synapse = app.state.synapse
    try:
        if user_id not in await synapse.get_joined_members(room_id, as_user_id=_bot(app)):
            await synapse.invite_user(room_id, user_id, as_user_id=_bot(app))
    except SynapseError as exc:
        # M_FORBIDDEN here is typically "already invited".
        logger.info("Invite of %s to %s: %s", user_id, room_id, exc)


async def follow(app: FastAPI, account: Account, identity: RemoteIdentity) -> tuple[RemoteAccount, bool]:
    """Follow ``identity`` as ``account``. Returns the remote account and
    whether this was a new follow (False: already following). Raises
    ``SynapseError``/``PdsError`` if the room or record can't be made."""
    repository = app.state.repository
    remote, created = await ensure_remote_account(app, identity)
    already = await repository.get_follow_rkey(account.did, remote.did) is not None
    if not already:
        pds = app.state.pds
        rkey = pds.next_record_key()
        record = {"$type": FOLLOW_COLLECTION, "subject": remote.did, "createdAt": now_timestamp()}
        await pds.apply_writes(account.did, [Write("create", FOLLOW_COLLECTION, rkey, record)])
        await repository.add_follow(account.did, remote.did, rkey)
        app.state.jetstream.refresh()
    await invite_to_room(app, remote.room_id, account.matrix_user_id)
    await add_room_to_space(app, account.matrix_user_id, remote.room_id)
    if created:
        run_in_background(app, backfill(app, remote, app.state.config.atproto.follow_backfill_count))
    return remote, not already


async def unfollow(app: FastAPI, account: Account, remote_did: str) -> bool:
    """Stop following. Returns False if ``account`` wasn't following."""
    repository = app.state.repository
    rkey = await repository.get_follow_rkey(account.did, remote_did)
    if rkey is None:
        return False
    try:
        await app.state.pds.apply_writes(account.did, [Write("delete", FOLLOW_COLLECTION, rkey)])
    except PdsError as exc:
        logger.info("Follow record %s of %s already gone: %s", rkey, account.did, exc.message)
    await repository.remove_follow(account.did, remote_did)
    app.state.jetstream.refresh()
    return True
