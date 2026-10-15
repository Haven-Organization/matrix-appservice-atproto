"""Finding other people's ATProto accounts: handle -> DID -> where they live.

Everything fetched here is at an address chosen by a stranger (a handle is
a hostname; a DID document names an arbitrary PDS URL), so every request
goes through ``fetch_public``, which refuses anything but HTTPS to a
publicly routable address -- otherwise following an account would be a way
to make the bridge call into the local network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

_HANDLE_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DID_RE = re.compile(r"^did:(plc:[a-z2-7]{24}|web:[A-Za-z0-9.%-]+)$")
_PROFILE_URL_RE = re.compile(r"^https?://(?:bsky\.app|[^/]+)/profile/([^/?#]+)")

# Used only as a fallback for handles verified by DNS TXT record, which
# would otherwise need a DNS client library.
_PUBLIC_APPVIEW = "https://public.api.bsky.app"


class IdentityError(Exception):
    """The account couldn't be found or its identity doesn't check out.
    The message is safe to show the user."""


@dataclass(frozen=True)
class RemoteIdentity:
    did: str
    handle: str
    pds: str


async def _is_public_host(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return False  # a bare IP is never a legitimate handle or PDS host
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


@dataclass(frozen=True)
class Fetched:
    content: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body as a JSON object; ``{}`` if it isn't one."""
        try:
            value = json.loads(self.content)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


async def fetch_public(
    http_client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None, max_bytes: int = 2_000_000
) -> Fetched | None:
    """GET ``url`` if it is HTTPS on a public address. ``None`` for a
    refused address, a network failure, a non-200 answer, or a body over
    ``max_bytes`` (which is not read past that point)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not await _is_public_host(parts.hostname):
        return None
    try:
        async with http_client.stream("GET", url, params=params, follow_redirects=False) as response:
            if response.status_code != 200:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
            content_type = response.headers.get("content-type", "application/octet-stream")
            return Fetched(b"".join(chunks), content_type.split(";", 1)[0].strip().lower())
    except httpx.HTTPError:
        return None


def parse_account_reference(text: str) -> str | None:
    """A handle or DID from what a user typed: ``alice.bsky.social``,
    ``@alice.bsky.social``, a DID, or a ``bsky.app/profile/...`` link."""
    text = text.strip()
    match = _PROFILE_URL_RE.match(text)
    if match:
        text = match.group(1)
    text = text.lstrip("@")
    if _DID_RE.match(text):
        return text
    text = text.lower()
    return text if _HANDLE_RE.match(text) and len(text) <= 253 else None


async def resolve_handle(http_client: httpx.AsyncClient, handle: str) -> str | None:
    response = await fetch_public(http_client, f"https://{handle}/.well-known/atproto-did", max_bytes=2048)
    if response is not None:
        did = response.text.strip()
        if _DID_RE.match(did):
            return did
    response = await fetch_public(
        http_client, f"{_PUBLIC_APPVIEW}/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle}
    )
    if response is not None:
        did = response.json().get("did", "")
        if _DID_RE.match(did):
            return did
    return None


async def resolve_did(http_client: httpx.AsyncClient, did: str, *, plc_directory: str) -> RemoteIdentity | None:
    """The account's current handle and PDS, from its DID document."""
    if did.startswith("did:plc:"):
        url = f"{plc_directory}/{did}"
    else:
        url = f"https://{did.removeprefix('did:web:')}/.well-known/did.json"
    response = await fetch_public(http_client, url)
    if response is None:
        return None
    document = response.json()
    if document.get("id") != did:
        return None
    pds = next(
        (
            service.get("serviceEndpoint")
            for service in document.get("service") or []
            if str(service.get("id", "")).endswith("#atproto_pds")
        ),
        None,
    )
    handle = next(
        (aka.removeprefix("at://") for aka in document.get("alsoKnownAs") or [] if str(aka).startswith("at://")), None
    )
    if not isinstance(pds, str) or not pds.startswith("https://") or not handle:
        return None
    return RemoteIdentity(did=did, handle=handle.lower(), pds=pds.rstrip("/"))


async def resolve_account(http_client: httpx.AsyncClient, reference: str, *, plc_directory: str) -> RemoteIdentity:
    """Resolve what a user typed to a verified identity, or raise
    ``IdentityError``. A handle only counts if the DID it points at claims
    that same handle back."""
    parsed = parse_account_reference(reference)
    if parsed is None:
        raise IdentityError(f'"{reference}" doesn\'t look like a Bluesky handle.')
    if parsed.startswith("did:"):
        identity = await resolve_did(http_client, parsed, plc_directory=plc_directory)
        if identity is None:
            raise IdentityError(f"Couldn't find the account {parsed}.")
        return identity
    did = await resolve_handle(http_client, parsed)
    if did is None:
        raise IdentityError(f"Couldn't find anyone with the handle {parsed}.")
    identity = await resolve_did(http_client, did, plc_directory=plc_directory)
    if identity is None or identity.handle != parsed:
        raise IdentityError(f"The handle {parsed} doesn't match the account it points to.")
    return identity


async def get_record(http_client: httpx.AsyncClient, pds: str, did: str, collection: str, rkey: str) -> dict | None:
    response = await fetch_public(
        http_client,
        f"{pds}/xrpc/com.atproto.repo.getRecord",
        params={"repo": did, "collection": collection, "rkey": rkey},
    )
    return response.json() or None if response is not None else None


async def list_records(http_client: httpx.AsyncClient, pds: str, did: str, collection: str, limit: int) -> list[dict]:
    """The account's newest ``limit`` records in ``collection``, newest first."""
    response = await fetch_public(
        http_client,
        f"{pds}/xrpc/com.atproto.repo.listRecords",
        params={"repo": did, "collection": collection, "limit": max(1, min(limit, 100))},
    )
    return list(response.json().get("records") or []) if response is not None else []


async def list_all_records(
    http_client: httpx.AsyncClient, pds: str, did: str, collection: str, *, max_records: int
) -> list[dict]:
    """Up to ``max_records`` of the account's records in ``collection``,
    newest first, a page at a time."""
    records: list[dict] = []
    cursor: str | None = None
    while len(records) < max_records:
        params: dict[str, Any] = {"repo": did, "collection": collection, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = await fetch_public(http_client, f"{pds}/xrpc/com.atproto.repo.listRecords", params=params)
        if response is None:
            break
        body = response.json()
        page = [record for record in body.get("records") or [] if isinstance(record, dict)]
        records += page
        cursor = body.get("cursor")
        if not page or not isinstance(cursor, str) or not cursor:
            break
    return records[:max_records]


async def get_blob(http_client: httpx.AsyncClient, pds: str, did: str, cid: str, *, max_bytes: int) -> bytes | None:
    response = await fetch_public(
        http_client, f"{pds}/xrpc/com.atproto.sync.getBlob", params={"did": did, "cid": cid}, max_bytes=max_bytes
    )
    return response.content if response is not None else None


def post_url(did: str, rkey: str) -> str:
    return f"https://bsky.app/profile/{quote(did, safe=':')}/post/{quote(rkey, safe='')}"
