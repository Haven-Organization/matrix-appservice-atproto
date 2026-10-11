"""Public ATProto HTTP surface served on every ``<label>.<bridge.domain>``.

For now that is only handle resolution. Both routes key off the request's
``Host`` header, so the reverse proxy in front of the bridge must forward
it unmodified (``proxy_set_header Host $host`` -- see
``deploy/nginx.conf.example``).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from bridge.atproto.handle_probe import PROBE_PATH_PREFIX
from bridge.atproto.handles import HandlePolicy

router = APIRouter()

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


@router.get("/.well-known/atproto-did", response_class=PlainTextResponse)
async def get_atproto_did(request: Request) -> PlainTextResponse:
    """ATProto's HTTPS handle resolution: the DID of whoever holds the
    handle this request was addressed to, as bare ``text/plain``."""
    policy: HandlePolicy = request.app.state.handle_policy
    label = policy.label_for_host(request.headers.get("host", ""))
    claim = await request.app.state.repository.get_handle_claim(label) if label else None
    if claim is None or claim.did is None:
        raise HTTPException(status_code=404, detail="No such handle")
    return PlainTextResponse(claim.did)


@router.get(PROBE_PATH_PREFIX + "{nonce}", response_class=PlainTextResponse, include_in_schema=False)
async def get_handle_probe(nonce: str, request: Request) -> PlainTextResponse:
    """Answer the bridge's own routing probe -- see
    ``bridge.atproto.handle_probe``."""
    prober = request.app.state.handle_prober
    if prober is None or not _NONCE_RE.match(nonce):
        raise HTTPException(status_code=404)
    host = request.headers.get("host", "").split(":", 1)[0]
    return PlainTextResponse(prober.answer(nonce, host))
