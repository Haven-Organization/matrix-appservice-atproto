"""TIDs: ATProto's timestamp identifiers.

Used as a commit's revision (``rev``) and as record keys. 13 characters of
a base32 alphabet chosen so that string order equals time order: 53 bits
of microseconds since the epoch followed by a 10-bit "clock id" that keeps
two writers in the same microsecond apart.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
TID_LENGTH = 13


def tid_from_micros(micros: int, clock_id: int) -> str:
    value = (micros & ((1 << 53) - 1)) << 10 | (clock_id & 0x3FF)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(60, -1, -5))


class TidClock:
    """Hands out TIDs that only ever go up, even if the wall clock steps
    back or two are asked for in the same microsecond -- a repository's
    ``rev`` must strictly increase from one commit to the next."""

    def __init__(self) -> None:
        self._clock_id = secrets.randbelow(1 << 10)
        self._last_micros = 0

    def next(self, *, after: str | None = None) -> str:
        micros = max(time.time_ns() // 1000, self._last_micros + 1)
        tid = tid_from_micros(micros, self._clock_id)
        if after is not None and tid <= after:
            micros = _micros_of(after) + 1
            tid = tid_from_micros(micros, self._clock_id)
        self._last_micros = micros
        return tid


def _micros_of(tid: str) -> int:
    value = 0
    for char in tid:
        value = value << 5 | _ALPHABET.index(char)
    return value >> 10
