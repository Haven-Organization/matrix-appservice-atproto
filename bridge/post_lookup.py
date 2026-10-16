"""Finding the post behind a Matrix event, and the event behind a post.

Liking, reposting and replying all start from a Matrix event and need the
ATProto post it stands for; mirroring a reply starts from a post address
and needs the Matrix event to thread under. Both directions have two
cases: a remote post mirrored into Matrix (``mirrored_posts``), or a local
user's own post made from a Matrix message (``records``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from fastapi import FastAPI

from bridge.atproto.identity import IdentityError, get_record, parse_account_reference, resolve_account, resolve_did
from bridge.matrix_records import POST_COLLECTION, link_urls
from bridge.ghost_ids import ghost_user_id
from bridge.pds import PdsError


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


logger = logging.getLogger(__name__)

# A post's web address, as bsky.app (and other ATProto web clients) write it.
_POST_LINK_RE = re.compile(r"^https?://[^/]+/profile/([^/?#]+)/post/([A-Za-z0-9._:~-]+)/?$")


async def find_linked_post(app: FastAPI, text: str) -> tuple[str, dict[str, str]] | None:
    """The first post ``text`` links to that can be found -- its link as
    written, and a reference to quote it by. ``None`` if there is none."""
    repository = app.state.repository
    for url in link_urls(text):
        match = _POST_LINK_RE.match(url)
        reference = parse_account_reference(match.group(1)) if match else None
        if match is None or reference is None:
            continue
        rkey = match.group(2)
        # A local account's post is in this bridge's own records.
        try:
            local = await app.state.pds.resolve_repo(reference)
        except PdsError:
            local = None
        if local is not None:
            row = await repository.get_record(local.did, POST_COLLECTION, rkey)
            uri, cid = f"at://{local.did}/{POST_COLLECTION}/{rkey}", row.cid if row is not None else ""
        else:
            # Best effort: a link that can't be followed up just stays a link.
            try:
                identity = await resolve_account(
                    app.state.http_client, reference, plc_directory=app.state.config.atproto.plc_directory
                )
                uri = f"at://{identity.did}/{POST_COLLECTION}/{rkey}"
                response = await get_record(app.state.http_client, identity.pds, identity.did, POST_COLLECTION, rkey)
                cid = str((response or {}).get("cid") or "")
            except IdentityError:
                continue
            except Exception:
                logger.warning("Looking up linked post %s failed", url, exc_info=True)
                continue
        if cid:
            return url, {"uri": uri, "cid": cid}
        logger.info("Linked post %s couldn't be found, so it isn't quoted", uri)
    return None
