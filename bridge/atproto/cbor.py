"""DAG-CBOR: the canonical binary encoding of everything ATProto signs.

A record's CID is the hash of its DAG-CBOR bytes, and a commit's signature
is over its DAG-CBOR bytes, so two implementations must produce the SAME
bytes for the same data. That is what the restrictions here are for: map
keys are strings sorted by length then bytewise, integers use their
shortest form, lengths are always definite, and the only tag is 42 (a link
to another block, i.e. a ``CID``).

Python mapping: ``dict`` (str keys), ``list``, ``str``, ``bytes``, ``int``,
``bool``, ``None`` and ``CID``. Floats are decoded (the format allows
them) but never encoded: ATProto's data model forbids them, so producing
one would always be a bug.
"""

from __future__ import annotations

import struct
from typing import Any

from bridge.atproto.cid import CID, CID_BYTE_LENGTH

_CID_TAG = 42


class CborError(ValueError):
    pass


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes((major << 5 | value,))
    if value < 1 << 8:
        return bytes((major << 5 | 24, value))
    if value < 1 << 16:
        return bytes((major << 5 | 25,)) + value.to_bytes(2, "big")
    if value < 1 << 32:
        return bytes((major << 5 | 26,)) + value.to_bytes(4, "big")
    if value < 1 << 64:
        return bytes((major << 5 | 27,)) + value.to_bytes(8, "big")
    raise CborError("Integer out of 64-bit range")


def _encode_into(value: Any, out: list[bytes]) -> None:
    # bool before int: True is an int in Python.
    if value is None:
        out.append(b"\xf6")
    elif value is True:
        out.append(b"\xf5")
    elif value is False:
        out.append(b"\xf4")
    elif isinstance(value, int):
        out.append(_head(0, value) if value >= 0 else _head(1, -1 - value))
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        out.append(_head(3, len(encoded)))
        out.append(encoded)
    elif isinstance(value, (bytes, bytearray)):
        out.append(_head(2, len(value)))
        out.append(bytes(value))
    elif isinstance(value, CID):
        # A link is tag 42 around a byte string: a 0x00 "identity
        # multibase" prefix, then the binary CID.
        out.append(_head(6, _CID_TAG))
        out.append(_head(2, CID_BYTE_LENGTH + 1))
        out.append(b"\x00" + value.to_bytes())
    elif isinstance(value, (list, tuple)):
        out.append(_head(4, len(value)))
        for item in value:
            _encode_into(item, out)
    elif isinstance(value, dict):
        keys = []
        for key in value:
            if not isinstance(key, str):
                raise CborError(f"Map keys must be strings, got {type(key).__name__}")
            keys.append((key.encode("utf-8"), key))
        keys.sort(key=lambda pair: (len(pair[0]), pair[0]))
        out.append(_head(5, len(keys)))
        for encoded, key in keys:
            out.append(_head(3, len(encoded)))
            out.append(encoded)
            _encode_into(value[key], out)
    else:
        raise CborError(f"Cannot encode {type(value).__name__} as DAG-CBOR")


def encode(value: Any) -> bytes:
    out: list[bytes] = []
    _encode_into(value, out)
    return b"".join(out)


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise CborError("Unexpected end of CBOR data")
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def argument(self, info: int) -> int:
        if info < 24:
            return info
        if info > 27:
            raise CborError("Indefinite-length and reserved CBOR forms are not allowed")
        return int.from_bytes(self.take(1 << (info - 24)), "big")

    def value(self) -> Any:
        initial = self.take(1)[0]
        major, info = initial >> 5, initial & 0x1F
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info == 22:
                return None
            if info == 27:
                return struct.unpack(">d", self.take(8))[0]
            raise CborError(f"Unsupported CBOR simple value {info}")
        argument = self.argument(info)
        if major == 0:
            return argument
        if major == 1:
            return -1 - argument
        if major == 2:
            return self.take(argument)
        if major == 3:
            try:
                return self.take(argument).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CborError("Invalid UTF-8 in CBOR text string") from exc
        if major == 4:
            return [self.value() for _ in range(argument)]
        if major == 5:
            result = {}
            for _ in range(argument):
                key = self.value()
                if not isinstance(key, str):
                    raise CborError("Map keys must be strings")
                result[key] = self.value()
            return result
        # major == 6
        if argument != _CID_TAG:
            raise CborError(f"Unsupported CBOR tag {argument}")
        link = self.value()
        if not isinstance(link, bytes) or link[:1] != b"\x00":
            raise CborError("Malformed CID link")
        try:
            return CID.from_bytes(link[1:])
        except ValueError as exc:
            raise CborError(str(exc)) from exc


def decode(data: bytes) -> Any:
    decoder = _Decoder(data)
    value = decoder.value()
    if decoder.pos != len(data):
        raise CborError("Trailing bytes after CBOR value")
    return value


def decode_prefix(data: bytes, offset: int = 0) -> tuple[Any, int]:
    """Decode one value starting at ``offset``; returns it and the offset
    just past it. For formats that concatenate values (event-stream frames)."""
    decoder = _Decoder(data)
    decoder.pos = offset
    return decoder.value(), decoder.pos
