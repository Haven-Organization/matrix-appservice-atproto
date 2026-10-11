"""Rebuilding a record's exact bytes on demand.

The bridge doesn't store record content (see ``bridge.repository``), but a
PDS has to be able to hand out any record, byte for byte, whenever the
network asks -- a relay re-downloading a repository, someone fetching one
post. So each record row carries a ``source``: a small JSON recipe naming a
*kind* and whatever that kind needs to produce the record again.

- ``{"kind": "inline", "record": {...}}`` -- the record itself, in JSON
  form. Only for records that ARE bookkeeping and have no Matrix content
  behind them: a follow, a like, a repost (a subject and a timestamp).
- Matrix-backed kinds (a post rebuilt from its Matrix event) are registered
  by the code that creates such records. Their conversion must be
  versioned and old versions kept forever: a record written under version
  1 must rebuild under version 1's rules for as long as it exists.

Rebuilding has three possible outcomes, and they must not be confused:

- The source is *gone* (the Matrix event was redacted): the record can
  never be served again and is deleted from the repository.
- The source now produces *different* bytes than the recorded CID (a
  profile room was renamed and the update hasn't been committed yet): the
  repository is brought up to date with a new commit.
- The source is *unavailable* right now (Synapse is restarting): an error.
  Nothing may be deleted on the strength of a failed lookup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bridge.atproto import cbor, lexjson
from bridge.atproto.cid import CID
from bridge.repository import RecordRow

logger = logging.getLogger(__name__)

# Given the source recipe, return the record as a data-model value, or None
# if its source is gone for good. Raise if it merely can't be reached.
Rebuilder = Callable[[dict[str, Any]], Awaitable[Any | None]]


class RecordUnavailable(Exception):
    """A record's source couldn't be consulted right now."""


@dataclass(frozen=True)
class Rebuilt:
    # None: the source is gone and the record with it.
    block: bytes | None
    # Whether `block` still hashes to the CID recorded at write time.
    matches: bool = False


def inline_source(record: Any) -> dict[str, Any]:
    return {"kind": "inline", "record": lexjson.to_json(record)}


async def _rebuild_inline(source: dict[str, Any]) -> Any:
    return lexjson.from_json(source["record"])


class RecordSources:
    def __init__(self) -> None:
        self._rebuilders: dict[str, Rebuilder] = {"inline": _rebuild_inline}

    def register(self, kind: str, rebuilder: Rebuilder) -> None:
        self._rebuilders[kind] = rebuilder

    async def rebuild(self, row: RecordRow) -> Rebuilt:
        source = json.loads(row.source)
        rebuilder = self._rebuilders.get(source.get("kind"))
        if rebuilder is None:
            raise RecordUnavailable(f"Record {row.did} {row.path} has unknown source kind {source.get('kind')!r}")
        try:
            record = await rebuilder(source)
        except Exception as exc:
            raise RecordUnavailable(f"Could not rebuild record {row.did} {row.path}: {exc!r}") from exc
        if record is None:
            return Rebuilt(None)
        block = cbor.encode(record)
        return Rebuilt(block, str(CID.for_cbor(block)) == row.cid)
