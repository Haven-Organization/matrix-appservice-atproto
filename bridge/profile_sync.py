"""Keeping an account's ``app.bsky.actor.profile`` in step with its Profile Room.

Display name and bio are the room's name and topic; the avatar is the
room's avatar; the banner is the room's MSC4221 banner state (set with the
``banner`` command, or by any client that knows that event).

Images are prepared once, when they change (see ``bridge.media``), and the
resulting blob reference is kept in the profile record's source alongside
the Matrix image it came from -- so an image is only reprocessed when the
room's actually changes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI

from bridge.atproto import lexjson
from bridge.atproto.cid import CID
from bridge.bridge_info import send_bridge_info
from bridge.matrix_records import PROFILE_COLLECTION, PROFILE_RKEY, profile_record_from_room, profile_source
from bridge.media import prepare_image
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
from bridge.repository import Account, BlobRow
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

_IMAGE_STATE_TYPES = {"avatar": "m.room.avatar", "banner": PROFILE_BANNER_STATE_TYPE}


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def prepare_profile_image(app: FastAPI, account: Account, mxc: str) -> dict[str, Any] | None:
    """Turn a Matrix image into a blob the profile can reference. Returns
    the ``{"mxc", "blob"}`` entry for the profile source, or ``None`` if
    the image can't be fetched or decoded."""
    synapse = app.state.synapse
    try:
        download = await synapse.download_media(mxc)
    except SynapseError:
        logger.warning("Could not download profile image %s", mxc, exc_info=True)
        return None
    image = await prepare_image(download.content) if download is not None else None
    if image is None:
        return None
    blob_mxc = mxc
    if image.reencoded:
        try:
            blob_mxc = await synapse.upload_media(image.data, image.mime_type, "profile.jpg", as_user_id=_bot(app))
        except SynapseError:
            logger.warning("Could not store re-encoded profile image", exc_info=True)
            return None
    cid = CID.for_raw(image.data)
    await app.state.repository.add_blob(BlobRow(account.did, str(cid), blob_mxc, image.mime_type, len(image.data)))
    blob = {"$type": "blob", "ref": cid, "mimeType": image.mime_type, "size": len(image.data)}
    return {"mxc": mxc, "blob": lexjson.to_json(blob)}


async def sync_profile_record(app: FastAPI, account: Account) -> None:
    """Make the account's profile record match its Profile Room."""
    if not account.profile_room_id:
        return
    repository = app.state.repository
    synapse = app.state.synapse
    bot = _bot(app)
    row = await repository.get_record(account.did, PROFILE_COLLECTION, PROFILE_RKEY)
    previous = json.loads(row.source) if row is not None else {}

    images: dict[str, Any] = {}
    for field, state_type in _IMAGE_STATE_TYPES.items():
        state = await synapse.get_room_state(account.profile_room_id, state_type, as_user_id=bot)
        mxc = (state or {}).get("url")
        entry = previous.get(field)
        if not mxc:
            entry = None
        elif entry is None or entry.get("mxc") != mxc:
            # Only when the room's image actually changed. If the new one
            # can't be used, the profile simply goes without.
            entry = await prepare_profile_image(app, account, mxc)
        images[field] = entry

    source = profile_source(account.profile_room_id, avatar=images["avatar"], banner=images["banner"])
    record = await profile_record_from_room(synapse, source, as_user_id=bot)
    await app.state.pds.put_record(account.did, PROFILE_COLLECTION, PROFILE_RKEY, record, source=source)
    # Keep the room's bridge info naming the account as it now appears.
    name = await synapse.get_room_state(account.profile_room_id, "m.room.name", as_user_id=bot)
    await send_bridge_info(
        app,
        room_id=account.profile_room_id,
        did=account.did,
        display_name=(name or {}).get("name") or app.state.pds.handle_of(account),
        avatar_mxc=(images["avatar"] or {}).get("mxc"),
    )
