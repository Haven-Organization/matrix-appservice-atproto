"""``did:plc`` operations: creating and updating an account's identity.

A did:plc identity is a chain of signed operations held by a public
directory (plc.directory). The first ("genesis") operation IS the
identity: the DID is derived from the hash of its signed bytes, so it
cannot be chosen or changed. Each operation states the account's current

- ``rotationKeys``: who may sign the NEXT operation (i.e. who controls the
  identity),
- ``verificationMethods.atproto``: the repository signing key,
- ``alsoKnownAs``: its handle, as ``at://<handle>``,
- ``services.atproto_pds``: where its PDS is.

This module only builds, signs and checks operations; submitting one to
the directory is the caller's (deliberate, irreversible) step.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from bridge.atproto import cbor
from bridge.atproto.cid import CID, b32encode
from bridge.atproto.keys import SigningKey, verify_signature

DEFAULT_PLC_DIRECTORY = "https://plc.directory"
_DID_PLC_LENGTH = 24


def build_operation(
    *,
    rotation_keys: list[str],
    signing_key: str,
    handle: str,
    pds_endpoint: str,
    prev: str | None = None,
) -> dict[str, Any]:
    """An unsigned operation. ``rotation_keys`` and ``signing_key`` are
    ``did:key`` strings; ``prev`` is the CID of the operation this one
    replaces, ``None`` for genesis."""
    return {
        "type": "plc_operation",
        "rotationKeys": rotation_keys,
        "verificationMethods": {"atproto": signing_key},
        "alsoKnownAs": [f"at://{handle}"],
        "services": {"atproto_pds": {"type": "AtprotoPersonalDataServer", "endpoint": pds_endpoint}},
        "prev": prev,
    }


def build_tombstone(previous_operation: dict[str, Any]) -> dict[str, Any]:
    """An unsigned operation that permanently deactivates the identity
    whose latest operation is ``previous_operation`` (signed). After it,
    the DID resolves to nothing and can never be updated again."""
    return {"type": "plc_tombstone", "prev": str(CID.for_cbor(cbor.encode(previous_operation)))}


def sign_operation(operation: dict[str, Any], rotation_key: SigningKey) -> dict[str, Any]:
    signature = rotation_key.sign(cbor.encode(operation))
    return {**operation, "sig": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}


def did_for_genesis(signed_operation: dict[str, Any]) -> str:
    digest = hashlib.sha256(cbor.encode(signed_operation)).digest()
    return "did:plc:" + b32encode(digest)[:_DID_PLC_LENGTH]


def verify_operation(signed_operation: dict[str, Any], rotation_keys: list[str]) -> bool:
    """Whether the operation is signed by one of ``rotation_keys`` -- its
    own for a genesis operation, the previous operation's otherwise."""
    unsigned = dict(signed_operation)
    signature_text = unsigned.pop("sig", None)
    if not isinstance(signature_text, str):
        return False
    try:
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
    except ValueError:
        return False
    data = cbor.encode(unsigned)
    return any(verify_signature(key, data, signature) for key in rotation_keys)
