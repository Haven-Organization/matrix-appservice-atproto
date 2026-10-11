"""Finding the post behind a Matrix event, and the event behind a post.

Liking, reposting and replying all start from a Matrix event and need the
ATProto post it stands for; mirroring a reply starts from a post address
and needs the Matrix event to thread under. Both directions have two
cases: a remote post mirrored into Matrix (``mirrored_posts``), or a local
user's own post made from a Matrix message (``records``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import FastAPI

from bridge.atproto.identity import get_record, resolve_did
from bridge.matrix_records import POST_COLLECTION
from bridge.ghost_ids import ghost_user_id


@dataclass(frozen=True)
class PostRef:
    uri: str
    cid: str
    # Where it is in Matrix, and who its author is there.
    room_id: str
    event_id: str
    sender: str
    # The root of its conversation; itself if it isn't a reply.
    root_uri: str
    root_cid: str

    def strong_ref(self) -> dict[str, str]:
        return {"uri": self.uri, "cid": self.cid}

    def root_ref(self) -> dict[str, str]:
        return {"uri": self.root_uri, "cid": self.root_cid}


def _did_of(uri: str) -> str:
    return uri.removeprefix("at://").split("/", 1)[0]


async def _local_ref(app: FastAPI, did: str, rkey: str, *, event_id: str | None = None) -> PostRef | None:
    repository = app.state.repository
    account = await repository.get_account(did)
    row = await repository.get_record(did, POST_COLLECTION, rkey) if account else None
    if account is None or row is None or not row.matrix_event_id:
        return None
    source = json.loads(row.source)
    uri = f"at://{did}/{POST_COLLECTION}/{rkey}"
    root = (source.get("reply") or {}).get("root") or {"uri": uri, "cid": row.cid}
    return PostRef(
        uri=uri,
        cid=row.cid,
        room_id=str(source.get("room_id") or account.profile_room_id or ""),
        event_id=event_id or row.matrix_event_id,
        sender=account.matrix_user_id,
        root_uri=str(root.get("uri", uri)),
        root_cid=str(root.get("cid", row.cid)),
    )


async def _root_from_network(app: FastAPI, uri: str, cid: str) -> tuple[str, str]:
    """The conversation root of a post nothing here recorded it for, read
    from the post itself. Falls back to the post being its own root."""
    parts = uri.removeprefix("at://").split("/")
    if len(parts) == 3:
        identity = await resolve_did(app.state.http_client, parts[0], plc_directory=app.state.config.atproto.plc_directory)
        if identity is not None:
            response = await get_record(app.state.http_client, identity.pds, parts[0], parts[1], parts[2])
            root = (((response or {}).get("value") or {}).get("reply") or {}).get("root") or {}
            if root.get("uri") and root.get("cid"):
                return str(root["uri"]), str(root["cid"])
    return uri, cid


async def _cid_from_network(app: FastAPI, uri: str) -> str:
    """A post's current record hash, asked of its own PDS -- for a post
    mirrored before its hash was being recorded."""
    parts = uri.removeprefix("at://").split("/")
    if len(parts) != 3:
        return ""
    identity = await resolve_did(app.state.http_client, parts[0], plc_directory=app.state.config.atproto.plc_directory)
    if identity is None:
        return ""
    response = await get_record(app.state.http_client, identity.pds, parts[0], parts[1], parts[2])
    return str((response or {}).get("cid") or "")


async def locate_post_by_uri(app: FastAPI, uri: str) -> PostRef | None:
    """Where the post ``uri`` is in Matrix, if anywhere."""
    repository = app.state.repository
    mirrored = await repository.get_mirrored_post(uri)
    if mirrored is not None and mirrored.event_ids:
        return PostRef(
            uri=uri,
            cid=mirrored.cid,
            room_id=mirrored.room_id,
            event_id=mirrored.event_ids[0],
            sender=ghost_user_id(app, _did_of(uri)),
            root_uri=mirrored.root_uri or uri,
            root_cid=mirrored.root_cid or mirrored.cid,
        )
    parts = uri.removeprefix("at://").split("/")
    if len(parts) == 3 and parts[1] == POST_COLLECTION:
        return await _local_ref(app, parts[0], parts[2])
    return None


async def locate_post_by_event(app: FastAPI, room_id: str, event_id: str, *, last_part: bool = False) -> PostRef | None:
    """The post the Matrix event ``event_id`` stands for, if it is one.

    A long message is several posts; ``last_part`` picks the final one
    (what a reply continues from) rather than the first (what a like or
    repost is about)."""
    repository = app.state.repository
    shown = await repository.get_shown_post_for_event(event_id)
    if shown is not None:
        uri, cid = shown
        mirrored = await repository.get_mirrored_post(uri)
        cid = cid or (mirrored.cid if mirrored is not None else "") or await _cid_from_network(app, uri)
        if not cid:
            return None  # can't be referenced without its hash
        if mirrored is not None and mirrored.root_uri:
            root_uri, root_cid = mirrored.root_uri, mirrored.root_cid
        elif mirrored is not None:
            root_uri, root_cid = uri, cid
        else:
            # Shown here as part of something else (a repost): nothing
            # recorded its conversation, so ask.
            root_uri, root_cid = await _root_from_network(app, uri, cid)
        return PostRef(uri, cid, room_id, event_id, ghost_user_id(app, _did_of(uri)), root_uri, root_cid)
    rows = [row for row in await repository.get_records_for_event(event_id) if row.collection == POST_COLLECTION]
    if not rows:
        return None
    row = rows[-1] if last_part else rows[0]
    return await _local_ref(app, row.did, row.rkey, event_id=event_id)
