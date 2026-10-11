"""The JSON form of ATProto data.

Records are DAG-CBOR on the wire between servers but JSON over the XRPC
API, and JSON can't express two of the data model's types directly. The
convention: a link is ``{"$link": "<cid>"}`` and a byte string is
``{"$bytes": "<base64>"}``.
"""

from __future__ import annotations

import base64
from typing import Any

from bridge.atproto.cid import CID


def to_json(value: Any) -> Any:
    if isinstance(value, CID):
        return {"$link": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii").rstrip("=")}
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_json(item) for item in value]
    return value


def from_json(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1:
            if isinstance(value.get("$link"), str):
                return CID.from_str(value["$link"])
            if isinstance(value.get("$bytes"), str):
                text = value["$bytes"]
                return base64.b64decode(text + "=" * (-len(text) % 4))
        return {key: from_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_json(item) for item in value]
    if isinstance(value, float):
        raise ValueError("ATProto data cannot contain floats")
    return value
