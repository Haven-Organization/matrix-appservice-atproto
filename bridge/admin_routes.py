"""Operator endpoints, for driving the PDS by hand.

A stopgap until accounts and posts are driven from Matrix (bot commands,
the AppService event stream): they let an operator create an account,
write records and ask a relay to crawl this PDS, against the RUNNING
bridge -- which matters, because only the running process can publish to
the firehose.

Disabled unless ``bridge.admin_token`` is set, and every call must present
it as ``Authorization: Bearer <token>``. They live under ``/_bridge/``,
which the public reverse proxy does not forward (see
``deploy/nginx.conf.example``).
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from bridge.atproto import lexjson
from bridge.handle_provisioning import HandleRejected
from bridge.onboarding import ensure_user_rooms
from bridge.pds import Pds, Write

router = APIRouter(prefix="/_bridge/admin", include_in_schema=False)


def _authorize(request: Request) -> Pds:
    token = request.app.state.config.bridge.admin_token
    if not token:
        raise HTTPException(status_code=404)
    presented = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not hmac.compare_digest(presented.encode(), token.encode()):
        raise HTTPException(status_code=401)
    return request.app.state.pds


@router.post("/accounts")
async def create_account(request: Request) -> dict[str, Any]:
    """``{"matrix_user_id": "@user:server"}`` -> the user's account,
    created and registered with the PLC directory if they had none."""
    pds = _authorize(request)
    body = await request.json()
    try:
        account = await pds.create_account(body["matrix_user_id"])
    except HandleRejected as exc:
        raise HTTPException(status_code=409, detail=exc.rejection.message) from exc
    return {"did": account.did, "handle": pds.handle_of(account), "matrix_user_id": account.matrix_user_id}


@router.post("/records")
async def write_records(request: Request) -> dict[str, Any]:
    """``{"did": ..., "writes": [{"action", "collection", "rkey"?, "record"?}]}``
    applied as one commit. Records written here are stored inline in the
    database (there is no Matrix event behind them), so this is for test
    content and bookkeeping records only."""
    pds = _authorize(request)
    body = await request.json()
    writes = [
        Write(
            action=item["action"],
            collection=item["collection"],
            rkey=item.get("rkey") or pds.next_record_key(),
            record=lexjson.from_json(item["record"]) if "record" in item else None,
        )
        for item in body["writes"]
    ]
    commit = await pds.apply_writes(body["did"], writes)
    return {"commit": str(commit), "paths": [f"{w.collection}/{w.rkey}" for w in writes]}


@router.post("/request-crawl")
async def request_crawl(request: Request) -> dict[str, Any]:
    """Ask each configured relay (``atproto.relays``) to subscribe to this
    PDS's firehose."""
    pds = _authorize(request)
    return {"results": await pds.request_crawl()}


@router.post("/onboard")
async def onboard(request: Request) -> dict[str, Any]:
    """``{"matrix_user_id": ...}`` -> make sure that user's space and
    notifications room exist, their rooms are filed in the space, and they
    are invited to all of them. What ``create profile`` does for someone
    who already has an identity; here for doing it on their behalf."""
    _authorize(request)
    body = await request.json()
    account = await request.app.state.repository.get_account_for_user(body["matrix_user_id"])
    if account is None:
        raise HTTPException(status_code=404, detail="No account for that user")
    return await ensure_user_rooms(request.app, account)
