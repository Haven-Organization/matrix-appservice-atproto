"""Remote ATProto accounts as Matrix users and rooms.

Each account someone here follows gets a ghost user (``@at_...`` by default) and a
Remote User Room: created by the bot, joined by the ghost, shared by every
local follower. The ghost's name and avatar, and the room's name, topic
and avatar, mirror the account's Bluesky profile. Rooms are MSC4501
profile rooms, the same as matrix-appservice-activitypub's (see
``bridge.msc4501``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from bridge.atproto.identity import (
    RemoteIdentity,
    fetch_public,
    get_blob,
    get_record,
    list_records,
    resolve_did,
)
from bridge.bridge_info import send_bridge_info
from bridge.ghost_ids import did_for_ghost, ghost_user_id
from bridge.matrix_records import POST_COLLECTION, PROFILE_COLLECTION, PROFILE_RKEY
from bridge.msc4501 import (
    EXTERNAL_HANDLE_FIELD,
    PROFILE_BANNER_STATE_TYPE,
    SOCIAL_PROFILE_ROOM_FIELD,
    SOCIAL_PROFILE_ROOM_TYPE,
    SOCIAL_PROFILE_USER_ID_POWER_LEVEL,
    SOCIAL_PROFILE_USER_ID_STATE_TYPE,
    external_handle_value,
)
from bridge.post_mirroring import REPOST_COLLECTION, mirror_post, mirror_repost
from bridge.repository import RemoteAccount
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

_AVATAR_MAX_BYTES = 2_000_000
_PUBLIC_APPVIEW = "https://public.api.bsky.app"
# Most replies of one conversation ever looked through in a thread backfill.
_THREAD_SCAN_LIMIT = 500
# How many of the newest posts a catch-up looks through.
_CATCH_UP_WINDOW = 30
_ROOM_NAME_STATE = "m.room.name"
_ROOM_TOPIC_STATE = "m.room.topic"
_ROOM_AVATAR_STATE = "m.room.avatar"


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def _image_mxc(app: FastAPI, identity: RemoteIdentity, ghost: str, ref: object, *, kind: str) -> str | None:
    """The Matrix copy of an image a remote profile refers to (``kind``:
    ``avatar`` or ``banner``), uploading it only if it isn't the one
    already copied -- so re-reading a profile that hasn't changed costs no
    download and leaves no duplicate in the media repository."""
    if not isinstance(ref, dict):
        return None
    cid = str((ref.get("ref") or {}).get("$link", ""))
    if not cid:
        return None
    repository = app.state.repository
    key = f"remote_{kind}:{identity.did}"
    known_cid, _, known_mxc = (await repository.get_value(key) or "").partition("|")
    if known_cid == cid and known_mxc:
        return known_mxc
    data = await get_blob(app.state.http_client, identity.pds, identity.did, cid, max_bytes=_AVATAR_MAX_BYTES)
    if data is None:
        return None
    mxc = await app.state.synapse.upload_media(data, str(ref.get("mimeType") or "image/jpeg"), kind, as_user_id=ghost)
    await repository.set_value(key, f"{cid}|{mxc}")
    return mxc


async def _apply_profile(
    app: FastAPI, identity: RemoteIdentity, ghost: str, room_id: str | None
) -> tuple[str, str | None, str | None, dict]:
    """Set the ghost's (and, given a room, the room's) name, avatar and
    topic from the remote profile. Returns the label, bio and avatar mxc
    used, and the profile record itself."""
    synapse = app.state.synapse
    config = app.state.config
    response = await get_record(app.state.http_client, identity.pds, identity.did, PROFILE_COLLECTION, PROFILE_RKEY)
    value = (response or {}).get("value") or {}
    display = str(value.get("displayName") or "").strip()
    label = f"{display} (@{identity.handle})" if display else f"@{identity.handle}"
    description = str(value.get("description") or "").strip() or None
    avatar_mxc = None
    try:
        await synapse.set_display_name(ghost, label)
        avatar_mxc = await _image_mxc(app, identity, ghost, value.get("avatar"), kind="avatar")
        if avatar_mxc:
            await synapse.set_avatar_url(ghost, avatar_mxc)
        if room_id is not None:
            bot = _bot(app)
            await synapse.send_state_event(room_id, _ROOM_NAME_STATE, {"name": label}, as_user_id=bot)
            await synapse.send_state_event(room_id, _ROOM_TOPIC_STATE, {"topic": description or ""}, as_user_id=bot)
            if avatar_mxc:
                await synapse.send_state_event(room_id, _ROOM_AVATAR_STATE, {"url": avatar_mxc}, as_user_id=bot)
    except SynapseError:
        logger.warning("Could not fully apply the profile of %s", identity.did, exc_info=True)
    if config.bridge.msc4503_external_handle in ("profile", "both"):
        try:
            await synapse.set_profile_field(
                ghost, EXTERNAL_HANDLE_FIELD, external_handle_value(config, handle=identity.handle)
            )
        except SynapseError:
            logger.info("Could not set %s for %s", EXTERNAL_HANDLE_FIELD, ghost, exc_info=True)
    return label, description, avatar_mxc, value


async def _apply_banner(app: FastAPI, identity: RemoteIdentity, ghost: str, room_id: str, profile: dict) -> None:
    """Mirror the remote profile's banner as the room's MSC4221 banner."""
    synapse = app.state.synapse
    try:
        mxc = await _image_mxc(app, identity, ghost, profile.get("banner"), kind="banner")
        if mxc:
            await synapse.send_state_event(room_id, PROFILE_BANNER_STATE_TYPE, {"url": mxc}, as_user_id=_bot(app))
    except SynapseError:
        logger.info("Could not set the banner of %s", room_id, exc_info=True)


async def _mark_profile_room(app: FastAPI, ghost: str, room_id: str) -> None:
    """MSC4501: say whose profile the room is, and (if enabled) point the
    ghost's own Matrix profile at it. Both best-effort."""
    synapse = app.state.synapse
    config = app.state.config
    try:
        await synapse.send_state_event(
            room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": ghost}, as_user_id=_bot(app)
        )
    except SynapseError:
        logger.info("Could not set %s in %s", SOCIAL_PROFILE_USER_ID_STATE_TYPE, room_id, exc_info=True)
    if config.bridge.set_msc4501_profile_room:
        try:
            # A room ID alone isn't routable: `via` says which server knows it.
            value = {"room_id": room_id, "via": [config.synapse.server_name]}
            await synapse.set_profile_field(ghost, SOCIAL_PROFILE_ROOM_FIELD, value)
        except SynapseError:
            logger.info("Could not set %s for %s", SOCIAL_PROFILE_ROOM_FIELD, ghost, exc_info=True)


async def apply_ghost_profiles(app: FastAPI) -> None:
    """Bring every account with a room here into line with the current
    settings, so ghosts and rooms made before a setting was turned on (or
    before the bridge set something at all) carry it too: the room's
    MSC4501 owner, and the ghost's MSC4501 profile room and MSC4503 handle
    profile fields. Only what differs is written."""
    config = app.state.config
    synapse = app.state.synapse
    for remote in await app.state.repository.list_remote_accounts():
        ghost, room_id = remote.ghost_user_id, remote.room_id
        try:
            if await synapse.get_room_state(room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, as_user_id=_bot(app)) != {
                "user_id": ghost
            }:
                await synapse.send_state_event(
                    room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": ghost}, as_user_id=_bot(app)
                )
        except SynapseError:
            logger.info("Could not set %s in %s", SOCIAL_PROFILE_USER_ID_STATE_TYPE, room_id, exc_info=True)
        wanted: dict[str, Any] = {}
        if config.bridge.set_msc4501_profile_room:
            wanted[SOCIAL_PROFILE_ROOM_FIELD] = {"room_id": room_id, "via": [config.synapse.server_name]}
        if config.bridge.msc4503_external_handle in ("profile", "both"):
            wanted[EXTERNAL_HANDLE_FIELD] = external_handle_value(config, handle=remote.handle)
        if not wanted:
            continue
        try:
            profile = await synapse.get_profile(ghost)
        except SynapseError:
            profile = {}
        for key, value in wanted.items():
            if profile.get(key) == value:
                continue
            try:
                await synapse.set_profile_field(ghost, key, value)
            except SynapseError:
                logger.info("Could not set %s for %s", key, ghost, exc_info=True)


async def ensure_ghost(app: FastAPI, identity: RemoteIdentity) -> tuple[str, str, str | None, str | None, dict]:
    """Make sure the account's ghost user exists and carries its current
    name and avatar. Returns the ghost's user ID, then the label, bio,
    avatar mxc and profile record it was given."""
    ghost = ghost_user_id(app, identity.did)
    try:
        await app.state.synapse.register_appservice_user(ghost[1:].split(":", 1)[0])
    except SynapseError as exc:
        if exc.errcode != "M_USER_IN_USE":
            raise
    label, description, avatar_mxc, profile = await _apply_profile(app, identity, ghost, None)
    return ghost, label, description, avatar_mxc, profile


async def create_remote_room(
    app: FastAPI,
    identity: RemoteIdentity,
    ghost: str,
    *,
    label: str,
    description: str | None,
    avatar_mxc: str | None,
    profile: dict,
    predecessor: dict[str, str] | None = None,
) -> str:
    """Create a Remote User Room for the account and dress it: MSC4501
    markings, banner, bridge info. Raises ``SynapseError`` on failure."""
    synapse = app.state.synapse
    room_id = await synapse.create_room(
        as_user_id=_bot(app),
        name=label,
        topic=description,
        invite=[ghost],
        avatar_mxc=avatar_mxc,
        room_type=SOCIAL_PROFILE_ROOM_TYPE,
        # The ghost whose room this is sits at the bot's own level;
        # followers stay at the default.
        power_level_users={ghost: 100},
        power_level_events={SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
        predecessor=predecessor,
    )
    await synapse.join_room(room_id, as_user_id=ghost)
    await _mark_profile_room(app, ghost, room_id)
    await _apply_banner(app, identity, ghost, room_id, profile)
    await send_bridge_info(app, room_id=room_id, did=identity.did, display_name=label, avatar_mxc=avatar_mxc)
    return room_id


async def ensure_remote_account(app: FastAPI, identity: RemoteIdentity) -> tuple[RemoteAccount, bool]:
    """The ghost and room for a remote account, created if this is the
    first time anyone here has dealt with it. Returns it and whether it
    was just created. Raises ``SynapseError`` if the room can't be made."""
    repository = app.state.repository
    existing = await repository.get_remote_account(identity.did)
    if existing is not None:
        if (existing.handle, existing.pds) != (identity.handle, identity.pds):
            existing = RemoteAccount(identity.did, identity.handle, identity.pds, existing.ghost_user_id, existing.room_id)
            await repository.save_remote_account(existing)
        return existing, False

    ghost, label, description, avatar_mxc, profile = await ensure_ghost(app, identity)
    room_id = await create_remote_room(
        app, identity, ghost, label=label, description=description, avatar_mxc=avatar_mxc, profile=profile
    )
    remote = RemoteAccount(identity.did, identity.handle, identity.pds, ghost, room_id)
    await repository.save_remote_account(remote)
    logger.info("Created Remote User Room %s for %s (%s)", room_id, identity.handle, identity.did)
    return remote, True


async def refresh_remote_account(app: FastAPI, remote: RemoteAccount, identity: RemoteIdentity) -> RemoteAccount:
    """Re-read the account's identity and profile and apply both."""
    updated = RemoteAccount(identity.did, identity.handle, identity.pds, remote.ghost_user_id, remote.room_id)
    await app.state.repository.save_remote_account(updated)
    label, _, avatar_mxc, profile = await _apply_profile(app, identity, updated.ghost_user_id, updated.room_id)
    await _apply_banner(app, identity, updated.ghost_user_id, updated.room_id, profile)
    await send_bridge_info(app, room_id=updated.room_id, did=identity.did, display_name=label, avatar_mxc=avatar_mxc)
    return updated


def _mark_key(did: str) -> str:
    return f"post_mark:{did}"


async def note_post_seen(app: FastAPI, did: str, rkey: str) -> None:
    """Remember ``rkey`` as the newest post of ``did`` dealt with, if it
    is. Record keys are timestamps, so they sort by time; ``catch_up``
    only looks at posts after this mark."""
    repository = app.state.repository
    mark = await repository.get_value(_mark_key(did))
    if mark is None or rkey > mark:
        await repository.set_value(_mark_key(did), rkey)


def _repost_mark_key(did: str) -> str:
    return f"repost_mark:{did}"


async def note_repost_seen(app: FastAPI, did: str, rkey: str) -> None:
    """As ``note_post_seen``, for the account's reposts."""
    repository = app.state.repository
    mark = await repository.get_value(_repost_mark_key(did))
    if mark is None or rkey > mark:
        await repository.set_value(_repost_mark_key(did), rkey)


async def _catch_up_reposts(app: FastAPI, remote: RemoteAccount) -> int:
    """Mirror reposts newer than the last one dealt with. The first look
    at an account only notes where its reposts are up to: unlike its own
    posts, an account's old reposts aren't backfilled."""
    records = await list_records(app.state.http_client, remote.pds, remote.did, REPOST_COLLECTION, _CATCH_UP_WINDOW)
    rkeys = {str(item.get("uri", "")).rsplit("/", 1)[-1]: item for item in records}
    mark = await app.state.repository.get_value(_repost_mark_key(remote.did))
    if mark is None:
        await app.state.repository.set_value(_repost_mark_key(remote.did), max(rkeys, default=""))
        return 0
    mirrored = 0
    for rkey in sorted(rkey for rkey in rkeys if rkey > mark):
        if isinstance(rkeys[rkey].get("value"), dict):
            try:
                mirrored += await mirror_repost(app, remote, rkey, rkeys[rkey]["value"])
            except Exception:
                logger.exception("Mirroring repost %s of %s failed", rkey, remote.did)
        await note_repost_seen(app, remote.did, rkey)
    return mirrored


async def _mirror_records(app: FastAPI, remote: RemoteAccount, records: list[dict]) -> int:
    """Mirror listed records oldest first, so they read in order."""
    mirrored = 0
    for item in sorted(records, key=lambda item: str(item.get("uri", ""))):
        rkey = str(item.get("uri", "")).rsplit("/", 1)[-1]
        if rkey and isinstance(item.get("value"), dict):
            try:
                mirrored += await mirror_post(app, remote, rkey, item["value"], cid=str(item.get("cid", "")))
            except Exception:
                logger.exception("Mirroring %s of %s failed", rkey, remote.did)
            await note_post_seen(app, remote.did, rkey)
    return mirrored


async def backfill(app: FastAPI, remote: RemoteAccount, count: int) -> int:
    """Mirror the account's ``count`` most recent posts. Returns how many
    were new."""
    records = await list_records(app.state.http_client, remote.pds, remote.did, POST_COLLECTION, count)
    mirrored = await _mirror_records(app, remote, records)
    await _catch_up_reposts(app, remote)
    return mirrored


async def catch_up(app: FastAPI, remote: RemoteAccount) -> int:
    """Mirror any post newer than the last one dealt with -- the safety
    net under the live feed (see ``bridge.jetstream``), which is not
    guaranteed to carry everything. Never reaches back before the mark,
    so it cannot drag in old posts the initial backfill left out."""
    mark = await app.state.repository.get_value(_mark_key(remote.did))
    if mark is None:
        # Nothing of theirs has ever been seen (they had no posts when
        # followed): whatever is there now is new.
        return await backfill(app, remote, app.state.config.atproto.follow_backfill_count)
    records = await list_records(app.state.http_client, remote.pds, remote.did, POST_COLLECTION, _CATCH_UP_WINDOW)
    newer = [item for item in records if str(item.get("uri", "")).rsplit("/", 1)[-1] > mark]
    mirrored = await _mirror_records(app, remote, newer) + await _catch_up_reposts(app, remote)
    if mirrored:
        logger.info("Caught up %d post(s) of %s the live feed didn't deliver", mirrored, remote.handle)
    return mirrored


def _thread_replies(thread: dict) -> list[dict]:
    """The reply posts under a ``getPostThread`` result, parents before
    children."""
    found: list[dict] = []
    queue = list(thread.get("replies") or [])
    while queue and len(found) < _THREAD_SCAN_LIMIT:
        node = queue.pop(0)
        post = node.get("post") if isinstance(node, dict) else None
        if isinstance(post, dict) and isinstance(post.get("record"), dict):
            found.append(post)
            queue.extend(node.get("replies") or [])
    return found


async def backfill_thread(app: FastAPI, remote: RemoteAccount, root_uri: str, root_event_id: str, count: int) -> int:
    """Mirror up to ``count`` replies to the post ``root_uri`` into a
    Matrix thread under ``root_event_id`` in ``remote``'s room, each sent
    by its own author's ghost. Returns how many were new.

    The conversation comes from the AppView: replies live in their
    authors' repositories, so no single PDS knows a post's replies."""
    config = app.state.config
    repository = app.state.repository
    synapse = app.state.synapse
    response = await fetch_public(
        app.state.http_client,
        f"{_PUBLIC_APPVIEW}/xrpc/app.bsky.feed.getPostThread",
        params={"uri": root_uri, "depth": 10, "parentHeight": 0},
        max_bytes=5_000_000,
    )
    thread = (response.json() if response is not None else {}).get("thread") or {}
    mirrored = 0
    for post in _thread_replies(thread):
        if mirrored >= count:
            break
        uri = str(post.get("uri", ""))
        did = str((post.get("author") or {}).get("did", ""))
        rkey = uri.rsplit("/", 1)[-1]
        if not did or not rkey or await repository.get_mirrored_post(uri) is not None:
            continue
        if await repository.get_account(did) is not None:
            continue  # a local user's own reply already lives in their Profile Room
        try:
            if did == remote.did:
                author = remote
            else:
                identity = await resolve_did(app.state.http_client, did, plc_directory=config.atproto.plc_directory)
                if identity is None:
                    continue
                ghost, *_ = await ensure_ghost(app, identity)
                if ghost not in await synapse.get_joined_members(remote.room_id, as_user_id=_bot(app)):
                    await synapse.invite_user(remote.room_id, ghost, as_user_id=_bot(app))
                    await synapse.join_room(remote.room_id, as_user_id=ghost)
                author = RemoteAccount(did, identity.handle, identity.pds, ghost, remote.room_id)
            mirrored += await mirror_post(
                app, author, rkey, post["record"], cid=str(post.get("cid", "")), thread_root=root_event_id
            )
        except Exception:
            logger.exception("Backfilling reply %s failed", uri)
    return mirrored


__all__ = [
    "backfill",
    "backfill_thread",
    "catch_up",
    "create_remote_room",
    "did_for_ghost",
    "apply_ghost_profiles",
    "ensure_ghost",
    "ensure_remote_account",
    "ghost_user_id",
    "note_post_seen",
    "note_repost_seen",
    "refresh_remote_account",
]
