"""The PDS's XRPC API: what the rest of the network calls on this server.

Read-only for now, and public by design -- these are the endpoints relays,
AppViews and other servers use to fetch and verify repositories:

- ``com.atproto.sync.*``: whole repositories and proofs as CAR files, and
  the firehose WebSocket.
- ``com.atproto.repo.*``: single records and listings as JSON.
- ``com.atproto.identity.resolveHandle``, ``com.atproto.server.describeServer``.
- ``/media/...``: Matrix files linked from posts (not XRPC, but served to
  the same audience).

There is no login or write API: nothing outside the bridge writes to a
repository. Writes come from Matrix, through ``bridge.pds.Pds``.

Errors follow the XRPC convention: HTTP 400 (502 if Matrix can't be reached) with
``{"error": "<Name>", "message": "..."}`` (see ``pds_error_handler``).
"""

from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from bridge.atproto import cbor, lexjson
from bridge.atproto.cid import CID
from bridge.atproto.firehose import encode_error_frame
from bridge.pds import Pds, PdsError
from bridge.synapse_client import SynapseError

router = APIRouter()

_CAR_MEDIA_TYPE = "application/vnd.ipld.car"


async def pds_error_handler(request: Request, exc: PdsError) -> JSONResponse:
    return JSONResponse({"error": exc.error, "message": exc.message}, status_code=exc.status)


def _pds(request: Request) -> Pds:
    return request.app.state.pds


def _pds_host(request: Request) -> str:
    return urlsplit(request.app.state.config.bridge.public_base_url).hostname or ""


@router.get("/xrpc/_health")
async def health() -> dict:
    return {"version": "matrix-appservice-atproto"}


@router.get("/.well-known/did.json")
async def server_did_document(request: Request) -> dict:
    """The PDS's own ``did:web`` identity (what ``describeServer`` names).
    Only on the PDS hostname: a did:web on a user's handle hostname would
    be a second, conflicting identity for that user."""
    host = _pds_host(request)
    if request.headers.get("host", "").split(":", 1)[0].lower() != host:
        raise HTTPException(status_code=404)
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": f"did:web:{host}",
        "service": [
            {
                "id": "#atproto_pds",
                "type": "AtprotoPersonalDataServer",
                "serviceEndpoint": request.app.state.config.bridge.public_base_url,
            }
        ],
    }


@router.get("/xrpc/com.atproto.server.describeServer")
async def describe_server(request: Request) -> dict:
    return {
        "did": f"did:web:{_pds_host(request)}",
        "availableUserDomains": [f".{request.app.state.config.bridge.domain}"],
        # Accounts come from Matrix; nobody can sign up through this API.
        "inviteCodeRequired": True,
    }


@router.get("/xrpc/com.atproto.identity.resolveHandle")
async def resolve_handle(request: Request, handle: str) -> dict:
    try:
        account = await _pds(request).resolve_repo(handle.lower())
    except PdsError:
        raise PdsError("HandleNotFound", "Unable to resolve handle") from None
    return {"did": account.did}


@router.get("/xrpc/com.atproto.repo.describeRepo")
async def describe_repo(request: Request, repo: str) -> dict:
    pds = _pds(request)
    account = await pds.resolve_repo(repo)
    rows = await request.app.state.repository.list_records(account.did)
    return {
        "handle": pds.handle_of(account),
        "did": account.did,
        "didDoc": pds.did_document(account),
        "collections": sorted({row.collection for row in rows}),
        "handleIsCorrect": True,
    }


@router.get("/xrpc/com.atproto.repo.getRecord")
async def get_record(request: Request, repo: str, collection: str, rkey: str, cid: str | None = None) -> dict:
    pds = _pds(request)
    account = await pds.resolve_repo(repo)
    found = await pds.get_record(account.did, collection, rkey)
    if found is None or (cid is not None and found[0].cid != cid):
        raise PdsError("RecordNotFound", f"Could not locate record: at://{account.did}/{collection}/{rkey}")
    row, block = found
    return {
        "uri": f"at://{account.did}/{row.path}",
        "cid": row.cid,
        "value": lexjson.to_json(cbor.decode(block)),
    }


@router.get("/xrpc/com.atproto.repo.listRecords")
async def list_records(
    request: Request, repo: str, collection: str, limit: int = 50, cursor: str | None = None, reverse: bool = False
) -> dict:
    pds = _pds(request)
    account = await pds.resolve_repo(repo)
    limit = max(1, min(limit, 100))
    rows = [row for row in await request.app.state.repository.list_records(account.did) if row.collection == collection]
    # Newest first by default: record keys are timestamp-ordered.
    rows.sort(key=lambda row: row.rkey, reverse=not reverse)
    if cursor is not None:
        rows = [row for row in rows if (row.rkey > cursor if reverse else row.rkey < cursor)]
    page = rows[:limit]
    records = []
    for row in page:
        found = await pds.get_record(account.did, row.collection, row.rkey)
        if found is not None:
            records.append(
                {"uri": f"at://{account.did}/{row.path}", "cid": row.cid, "value": lexjson.to_json(cbor.decode(found[1]))}
            )
    result: dict = {"records": records}
    if len(rows) > limit:
        result["cursor"] = page[-1].rkey
    return result


@router.get("/xrpc/com.atproto.sync.getRepo")
async def sync_get_repo(request: Request, did: str) -> Response:
    # `since` (send only what changed after a revision) is optional for a
    # server to honour; the full repository is always a valid answer.
    pds = _pds(request)
    account = await pds.resolve_repo(did)
    return Response(await pds.repo_car(account.did), media_type=_CAR_MEDIA_TYPE)


@router.get("/xrpc/com.atproto.sync.getRecord")
async def sync_get_record(request: Request, did: str, collection: str, rkey: str) -> Response:
    pds = _pds(request)
    account = await pds.resolve_repo(did)
    return Response(await pds.record_proof_car(account.did, collection, rkey), media_type=_CAR_MEDIA_TYPE)


@router.get("/xrpc/com.atproto.sync.getBlocks")
async def sync_get_blocks(request: Request, did: str, cids: list[str] = Query()) -> Response:
    pds = _pds(request)
    account = await pds.resolve_repo(did)
    try:
        wanted = [CID.from_str(cid) for cid in cids]
    except ValueError:
        raise PdsError("InvalidRequest", "Malformed cid") from None
    return Response(await pds.blocks_car(account.did, wanted), media_type=_CAR_MEDIA_TYPE)


@router.get("/xrpc/com.atproto.sync.getLatestCommit")
async def sync_get_latest_commit(request: Request, did: str) -> dict:
    account = await _pds(request).resolve_repo(did)
    return {"cid": account.commit_cid, "rev": account.rev}


@router.get("/xrpc/com.atproto.sync.getRepoStatus")
async def sync_get_repo_status(request: Request, did: str) -> dict:
    account = await _pds(request).resolve_repo(did)
    return {"did": account.did, "active": account.active, "rev": account.rev}


@router.get("/xrpc/com.atproto.sync.listRepos")
async def sync_list_repos(request: Request, limit: int = 500, cursor: str | None = None) -> dict:
    limit = max(1, min(limit, 1000))
    accounts = [a for a in await request.app.state.repository.list_accounts() if cursor is None or a.did > cursor]
    page = accounts[:limit]
    result: dict = {
        "repos": [{"did": a.did, "head": a.commit_cid, "rev": a.rev, "active": a.active} for a in page]
    }
    if len(accounts) > limit:
        result["cursor"] = page[-1].did
    return result


# Blobs and published files are other people's uploads served from this
# origin: never let a browser sniff or run them. Opening a video or audio
# file directly gives a page whose player loads the file itself and styles
# its controls inline, so those two are allowed (as Synapse does for its
# media); nothing can run scripts.
_UNTRUSTED_CONTENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; sandbox",
}
_INLINE_SAFE_TYPE_PREFIXES = ("image/", "video/", "audio/")


@router.get("/xrpc/com.atproto.sync.listBlobs")
async def sync_list_blobs(request: Request, did: str) -> dict:
    account = await _pds(request).resolve_repo(did)
    return {"cids": [blob.cid for blob in await request.app.state.repository.list_blobs(account.did)]}


@router.get("/xrpc/com.atproto.sync.getBlob")
async def sync_get_blob(request: Request, did: str, cid: str) -> Response:
    """A blob's bytes, fetched from the Matrix media repository and checked
    against the CID before being served."""
    account = await _pds(request).resolve_repo(did)
    blob = await request.app.state.repository.get_blob(account.did, cid)
    if blob is None:
        raise PdsError("BlobNotFound", "Blob not found")
    try:
        download = await request.app.state.synapse.download_media(blob.mxc)
    except (SynapseError, httpx.HTTPError) as exc:
        raise PdsError("UpstreamFailure", "Blob is temporarily unavailable", status=502) from exc
    if download is None or str(CID.for_raw(download.content)) != blob.cid:
        raise PdsError("BlobNotFound", "Blob not found")
    return Response(download.content, media_type=blob.mime_type, headers=_UNTRUSTED_CONTENT_HEADERS)


@router.get("/media/{server_name}/{media_id}")
async def get_published_media(request: Request, server_name: str, media_id: str) -> Response:
    """A Matrix file linked from a post (see ``bridge.profile_posts``).
    Only media explicitly published is reachable here."""
    mxc = f"mxc://{server_name}/{media_id}"
    if await request.app.state.repository.get_published_media(mxc) is None:
        raise HTTPException(status_code=404)
    try:
        download = await request.app.state.synapse.download_media(mxc)
    except (SynapseError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502) from exc
    if download is None:
        raise HTTPException(status_code=404)
    headers = dict(_UNTRUSTED_CONTENT_HEADERS)
    disposition = download.content_disposition
    if disposition and not download.content_type.startswith(_INLINE_SAFE_TYPE_PREFIXES):
        # Anything a browser might render as a document is downloaded instead.
        disposition = "attachment" + disposition[disposition.index(";") :] if ";" in disposition else "attachment"
    if disposition:
        headers["Content-Disposition"] = disposition
    headers["Accept-Ranges"] = "bytes"
    body = download.content
    span = _byte_range(request.headers.get("range"), len(body))
    if span is None:
        return Response(body, media_type=download.content_type, headers=headers)
    if span is False:
        headers["Content-Range"] = f"bytes */{len(body)}"
        return Response(status_code=416, headers=headers)
    start, end = span
    headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
    return Response(body[start : end + 1], status_code=206, media_type=download.content_type, headers=headers)


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None | Literal[False]:
    """The span a single-range ``Range`` header asks for, as inclusive
    offsets. ``None`` means serve the whole file (no header, or one this
    doesn't handle, such as several ranges); ``False`` means the range lies
    outside the file. Video players seek with these, and some won't play
    a file served without them."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[len("bytes=") :].strip().partition("-")
    try:
        if not first:  # "-N": the last N bytes
            length = int(last)
            if length <= 0:
                return False
            return max(size - length, 0), size - 1
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return False
    return start, end


@router.websocket("/xrpc/com.atproto.sync.subscribeRepos")
async def subscribe_repos(websocket: WebSocket, cursor: int | None = None) -> None:
    firehose = websocket.app.state.firehose
    await websocket.accept()
    try:
        if cursor is not None and cursor > await websocket.app.state.repository.current_firehose_seq():
            await websocket.send_bytes(encode_error_frame("FutureCursor", "Cursor in the future."))
            await websocket.close()
            return
        async for frame in firehose.subscribe(cursor):
            await websocket.send_bytes(frame)
        await websocket.close()
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # The server is shutting down and cutting this stream off (see
        # main.py's timeout_graceful_shutdown). That is the expected way
        # for a subscription to end, not an error worth a traceback.
        pass
