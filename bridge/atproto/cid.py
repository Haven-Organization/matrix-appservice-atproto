"""Content identifiers (CIDs) as ATProto uses them.

Every block in a repository -- a record, a tree node, a commit -- and every
blob is addressed by the hash of its bytes. ATProto pins this down to one
shape: CIDv1, sha2-256, with codec ``dag-cbor`` for repository blocks and
``raw`` for blobs. Nothing else is produced here, and nothing else is
accepted when parsing.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

CODEC_DAG_CBOR = 0x71
CODEC_RAW = 0x55
_SHA2_256 = 0x12
_DIGEST_LENGTH = 32
CID_BYTE_LENGTH = 4 + _DIGEST_LENGTH


def b32encode(data: bytes) -> str:
    """Lowercase, unpadded base32 -- the form CIDs, did:plc and TIDs' cousins all use."""
    return base64.b32encode(data).decode("ascii").lower().rstrip("=")


def b32decode(text: str) -> bytes:
    return base64.b32decode(text.upper() + "=" * (-len(text) % 8))


@dataclass(frozen=True, order=True)
class CID:
    codec: int
    digest: bytes

    def __post_init__(self) -> None:
        if self.codec not in (CODEC_DAG_CBOR, CODEC_RAW) or len(self.digest) != _DIGEST_LENGTH:
            raise ValueError("Not a CIDv1 sha2-256 dag-cbor/raw CID")

    @classmethod
    def for_cbor(cls, block: bytes) -> CID:
        return cls(CODEC_DAG_CBOR, hashlib.sha256(block).digest())

    @classmethod
    def for_raw(cls, blob: bytes) -> CID:
        return cls(CODEC_RAW, hashlib.sha256(blob).digest())

    @classmethod
    def from_bytes(cls, data: bytes) -> CID:
        if len(data) != CID_BYTE_LENGTH or data[0] != 1 or data[2] != _SHA2_256 or data[3] != _DIGEST_LENGTH:
            raise ValueError("Not a CIDv1 sha2-256 CID")
        return cls(data[1], data[4:])

    @classmethod
    def from_str(cls, text: str) -> CID:
        if not text.startswith("b"):
            raise ValueError("Only base32 ('b...') CID strings are supported")
        return cls.from_bytes(b32decode(text[1:]))

    def to_bytes(self) -> bytes:
        return bytes((1, self.codec, _SHA2_256, _DIGEST_LENGTH)) + self.digest

    def __str__(self) -> str:
        return "b" + b32encode(self.to_bytes())

    def __repr__(self) -> str:
        return f"CID({self})"
