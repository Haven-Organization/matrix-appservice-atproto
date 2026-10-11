"""CAR v1 files: how repository blocks are shipped.

A CAR ("content-addressable archive") is a header naming the root block
followed by ``(CID, bytes)`` pairs, each length-prefixed with a varint.
``com.atproto.sync.getRepo`` returns a whole repository as one, and every
commit event on the firehose carries its new blocks as one.
"""

from __future__ import annotations

from typing import Iterable

from bridge.atproto import cbor
from bridge.atproto.cid import CID, CID_BYTE_LENGTH


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("Truncated CAR file")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def write_car(root: CID | None, blocks: Iterable[tuple[CID, bytes]]) -> bytes:
    """``root`` is ``None`` for a bare bag of blocks with no entry point
    (``com.atproto.sync.getBlocks``)."""
    header = cbor.encode({"roots": [root] if root is not None else [], "version": 1})
    out = [_varint(len(header)), header]
    for cid, block in blocks:
        out.append(_varint(CID_BYTE_LENGTH + len(block)))
        out.append(cid.to_bytes())
        out.append(block)
    return b"".join(out)


def read_car(data: bytes) -> tuple[list[CID], dict[CID, bytes]]:
    """Parse a CAR into its roots and blocks. Blocks' hashes are NOT
    checked here; a caller that doesn't trust the source must do that."""
    length, pos = _read_varint(data, 0)
    header = cbor.decode(data[pos : pos + length])
    pos += length
    blocks: dict[CID, bytes] = {}
    while pos < len(data):
        length, pos = _read_varint(data, pos)
        cid = CID.from_bytes(data[pos : pos + CID_BYTE_LENGTH])
        blocks[cid] = data[pos + CID_BYTE_LENGTH : pos + length]
        pos += length
    return header["roots"], blocks
