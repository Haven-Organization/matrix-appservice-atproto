"""The firehose: ``com.atproto.sync.subscribeRepos``.

How anything this PDS hosts reaches the rest of the network. A relay holds
one WebSocket open and receives a numbered stream of events: every commit
(with the new blocks attached), plus identity and account-status changes.
If it disconnects it comes back with the last sequence number it saw
(``cursor``) and expects to be caught up.

Commit events contain record content, so their payloads are kept only in
memory (a bounded buffer), never in the database. That is the one place
this differs from a conventional PDS: after a restart, or for a cursor
older than the buffer, the original events can't be replayed. Instead each
account that changed in the gap gets a ``#sync`` event -- "this repository
is now at commit X" -- which carries no record content and tells the
consumer to re-fetch that repository if it's behind. Sequence numbers
themselves are persisted, so they never repeat or go backwards.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from bridge.atproto import cbor
from bridge.atproto.car import write_car
from bridge.atproto.cid import CID
from bridge.repository import BridgeRepository

_SUBSCRIBER_QUEUE_SIZE = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def encode_frame(event_type: str, body: dict[str, Any]) -> bytes:
    """A message frame: two concatenated DAG-CBOR values, header then body."""
    return cbor.encode({"op": 1, "t": event_type}) + cbor.encode(body)


def encode_error_frame(error: str, message: str) -> bytes:
    return cbor.encode({"op": -1}) + cbor.encode({"error": error, "message": message})


class _SubscriberTooSlow(Exception):
    pass


class Firehose:
    def __init__(self, repository: BridgeRepository, *, buffer_size: int = 5000) -> None:
        self._repository = repository
        self._buffer: deque[tuple[int, bytes]] = deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        # Held while a sequence number is allocated and its frame published,
        # so frames reach the buffer and every subscriber in seq order.
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _emit(self, did: str, event_type: str, body: dict[str, Any]) -> int:
        async with self._lock:
            seq = await self._repository.next_firehose_seq(did, event_type)
            frame = encode_frame(event_type, {**body, "seq": seq, "time": _now()})
            self._buffer.append((seq, frame))
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Drop it rather than buffer without bound; it will
                    # reconnect with its cursor.
                    self._subscribers.discard(queue)
                    queue.get_nowait()
                    queue.put_nowait(None)
            return seq

    async def emit_commit(
        self,
        *,
        did: str,
        commit: CID,
        rev: str,
        since: str | None,
        prev_data: CID | None,
        ops: list[dict[str, Any]],
        blocks: list[tuple[CID, bytes]],
    ) -> int:
        body: dict[str, Any] = {
            "repo": did,
            "commit": commit,
            "rev": rev,
            "since": since,
            "blocks": write_car(commit, blocks),
            "ops": ops,
            "blobs": [],
            # Deprecated but still-required fields of the event schema.
            "rebase": False,
            "tooBig": False,
        }
        if prev_data is not None:
            body["prevData"] = prev_data
        return await self._emit(did, "#commit", body)

    async def emit_identity(self, did: str, handle: str) -> int:
        return await self._emit(did, "#identity", {"did": did, "handle": handle})

    async def emit_account(self, did: str, *, active: bool, status: str | None = None) -> int:
        body: dict[str, Any] = {"did": did, "active": active}
        if status is not None:
            body["status"] = status
        return await self._emit(did, "#account", body)

    async def _sync_frame(self, did: str, seq: int) -> bytes | None:
        account = await self._repository.get_account(did)
        if account is None:
            return None
        commit = CID.from_str(account.commit_cid)
        body = {
            "did": did,
            "rev": account.rev,
            "blocks": write_car(commit, [(commit, account.commit_block)]),
            "seq": seq,
            "time": _now(),
        }
        return encode_frame("#sync", body)

    async def subscribe(self, cursor: int | None) -> AsyncIterator[bytes]:
        """Yield frames for one consumer: catch-up from ``cursor`` (if
        given), then live events until the consumer goes away."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        async with self._lock:
            backlog: list[bytes] = []
            if cursor is not None:
                buffered = [(seq, frame) for seq, frame in self._buffer if seq > cursor]
                # Everything after the cursor that is no longer (or was
                # never, across a restart) in the buffer.
                upto = buffered[0][0] - 1 if buffered else await self._repository.current_firehose_seq()
                latest_missed: dict[str, int] = {}
                for event in await self._repository.firehose_events_between(cursor, upto):
                    latest_missed[event.did] = event.seq
                for did, seq in sorted(latest_missed.items(), key=lambda item: item[1]):
                    frame = await self._sync_frame(did, seq)
                    if frame is not None:
                        backlog.append(frame)
                backlog.extend(frame for _, frame in buffered)
            self._subscribers.add(queue)
        try:
            for frame in backlog:
                yield frame
            while True:
                frame = await queue.get()
                if frame is None:
                    raise _SubscriberTooSlow
                yield frame
        except _SubscriberTooSlow:
            yield encode_error_frame("ConsumerTooSlow", "Stream consumer too slow")
        finally:
            self._subscribers.discard(queue)
