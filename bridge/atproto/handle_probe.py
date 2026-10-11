"""Live check that ``<label>.<bridge.domain>`` actually reaches this bridge.

The reserved-name lists (``bridge.atproto.handles``) only know about
subdomains someone thought to write down. This catches the rest: before a
handle is claimed, the bridge requests a challenge from that exact hostname
over the public internet path and checks that the answer came from itself.

- An explicit DNS record or a dedicated reverse-proxy vhost for that name
  (a wiki, a mail UI, anything) answers instead of the bridge -> the name
  is taken by something else, refuse it.
- Nothing answers at all -> the wildcard DNS record or certificate isn't
  in place, and the handle would fail ATProto verification anyway.

Comparing DNS answers against the wildcard's would miss the common case of
a real subdomain served by the same reverse proxy on the same IP; asking
the hostname itself doesn't.

The answer is an HMAC over the nonce AND the ``Host`` header the bridge
saw, so a pass also proves the proxy forwards ``Host`` unmodified -- which
``/.well-known/atproto-did`` depends on to know whose DID to return.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

PROBE_PATH_PREFIX = "/_bridge/handle-probe/"


class ProbeResult(str, Enum):
    ROUTED = "routed"
    # Something answered, but it wasn't this bridge.
    FOREIGN = "foreign"
    # DNS, TCP or TLS failure: nothing usable answers at that hostname.
    UNREACHABLE = "unreachable"


class HandleRoutingProber:
    def __init__(self, http_client: httpx.AsyncClient, domain: str) -> None:
        self._http_client = http_client
        self._domain = domain
        # Per-process: a probe is only ever answered by the same running
        # instance that sent it, so nothing needs to persist or be shared.
        self._secret = secrets.token_bytes(32)

    def answer(self, nonce: str, host: str) -> str:
        """What the probe route returns for ``nonce`` as requested via ``host``."""
        message = f"{nonce}\n{host.lower()}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    async def probe(self, label: str) -> ProbeResult:
        host = f"{label}.{self._domain}"
        nonce = secrets.token_hex(16)
        try:
            # Redirects are deliberately not followed: a foreign vhost that
            # redirects everything to the main site must not end up being
            # answered by the bridge under a different hostname.
            response = await self._http_client.get(
                f"https://{host}{PROBE_PATH_PREFIX}{nonce}", follow_redirects=False
            )
        except httpx.HTTPError as exc:
            logger.info("Handle probe for %s failed to connect: %r", host, exc)
            return ProbeResult.UNREACHABLE
        if response.status_code == 200 and hmac.compare_digest(response.text.strip(), self.answer(nonce, host)):
            return ProbeResult.ROUTED
        logger.info("Handle probe for %s was answered by something else (HTTP %d)", host, response.status_code)
        return ProbeResult.FOREIGN
