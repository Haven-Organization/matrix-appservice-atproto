"""Inter-service authentication: acting for an account at another service.

When a PDS asks another ATProto service (an AppView, say) for something on
a user's behalf, it presents a short-lived JWT signed with that account's
own repository signing key. The service checks the signature against the
key in the account's DID document, so no password or session is involved.
The token names the one method it is good for (``lxm``) and the service it
is addressed to (``aud``).
"""

from __future__ import annotations

import base64
import json
import secrets
import time

from bridge.atproto.keys import SigningKey

_TOKEN_LIFETIME_SECONDS = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def service_auth_token(key: SigningKey, *, issuer: str, audience: str, method: str) -> str:
    """A JWT letting the bearer call ``method`` on ``audience`` as ``issuer``."""
    now = int(time.time())
    header = {"typ": "JWT", "alg": "ES256K"}
    payload = {
        "iss": issuer,
        "aud": audience,
        "lxm": method,
        "iat": now,
        "exp": now + _TOKEN_LIFETIME_SECONDS,
        "jti": secrets.token_hex(16),
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode('ascii')))}"
