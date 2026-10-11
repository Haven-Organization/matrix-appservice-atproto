"""Receiving what followed accounts do.

ATProto has no inbox: nothing is delivered to this server when someone it
follows posts. Instead the bridge subscribes to Jetstream -- a JSON feed of
everything happening on the network -- asking it to send only the accounts
followed here, and acts on each event as it arrives: a new post is
mirrored (see ``bridge.post_mirroring``), as is a repost; a deleted one is
redacted; a like of a post that is in Matrix becomes a reaction (see
``bridge.reaction_mirroring``); and a changed profile or handle is
applied to the account's ghost and room.

The position in the feed is saved, so what happened while the bridge was
down is picked up when it reconnects (as far back as Jetstream retains).
Replays are harmless: a post already mirrored is skipped.

The feed is not the whole story, though. It can run well behind (a new
account's events have been seen arriving a quarter of an hour late) and
anything it carried before the saved position is never replayed, so
``catch_up_loop`` also asks each followed account's PDS directly, every
few minutes, for anything newer than the last post mirrored, and
periodically re-reads its profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets
from fastapi import FastAPI

from bridge.atproto.identity import resolve_did
from bridge.matrix_records import POST_COLLECTION, PROFILE_COLLECTION
from bridge.notification_poller import nudge_notifications
from bridge.post_mirroring import REPOST_COLLECTION, mirror_post, mirror_repost, unmirror, unmirror_post
from bridge.reaction_mirroring import LIKE_EMOJI, REPOST_EMOJI, mirror_reaction, unmirror_reaction
from bridge.remote_accounts import catch_up, note_post_seen, note_repost_seen, refresh_remote_account
from bridge.repository import RemoteAccount

logger = logging.getLogger(__name__)

LIKE_COLLECTION = "app.bsky.feed.like"
_CURSOR_KEY = "jetstream_cursor"
_RECONNECT_DELAY = 5.0
_MAX_RECONNECT_DELAY = 120.0
# A connection that held at least this long counts as having worked.
_STABLE_CONNECTION = 60.0
# On reconnect, step back a little so nothing in flight at the moment of
# disconnection is missed.
_CURSOR_REWIND_US = 5_000_000
# While connected but hearing nothing (the accounts followed are quiet),
# the saved position is moved up to "a minute ago" this often -- so a
# reconnect after a long quiet spell doesn't replay hours of the network.
_IDLE_CURSOR_INTERVAL = 30.0
_IDLE_CURSOR_MARGIN_US = 60_000_000
_FIRST_CATCH_UP_DELAY = 20.0
# Every this many catch-up rounds is also a profile refresh.
_PROFILE_REFRESH_EVERY = 12


async def _refresh(app: FastAPI, remote: RemoteAccount) -> None:
    """Re-read the account's identity and profile, and look for posts the
    feed may have skipped around whatever just changed."""
    identity = await resolve_did(app.state.http_client, remote.did, plc_directory=app.state.config.atproto.plc_directory)
    if identity is not None:
        remote = await refresh_remote_account(app, remote, identity)
    await catch_up(app, remote)


async def _react(app: FastAPI, remote: RemoteAccount, collection: str, rkey: str, record: dict, emoji: str) -> None:
    """A followed account liked or reposted something: if that post is in
    Matrix, show it as a reaction from their ghost."""
    subject = record.get("subject") if isinstance(record.get("subject"), dict) else {}
    await mirror_reaction(
        app,
        actor_did=remote.did,
        record_uri=f"at://{remote.did}/{collection}/{rkey}",
        subject_uri=str(subject.get("uri", "")),
        emoji=emoji,
    )


async def handle_event(app: FastAPI, event: dict[str, Any], accounts: dict[str, RemoteAccount]) -> None:
    """Act on one Jetstream event about a followed account. Anything else
    is ignored."""
    remote = accounts.get(str(event.get("did", "")))
    if remote is None:
        return
    kind = event.get("kind")
    if kind in ("identity", "account"):
        # A handle change, or the account's status changing.
        await _refresh(app, remote)
        return
    commit = event.get("commit")
    if kind != "commit" or not isinstance(commit, dict):
        return
    collection = commit.get("collection")
    operation = commit.get("operation")
    rkey = str(commit.get("rkey", ""))
    if collection == PROFILE_COLLECTION and operation in ("create", "update"):
        await _refresh(app, remote)
    elif collection == POST_COLLECTION and operation == "create" and isinstance(commit.get("record"), dict):
        await mirror_post(app, remote, rkey, commit["record"], cid=str(commit.get("cid", "")))
        await note_post_seen(app, remote.did, rkey)
        await nudge_notifications(app, commit["record"])
    elif collection == POST_COLLECTION and operation == "delete":
        await unmirror_post(app, remote, rkey)
    elif collection == REPOST_COLLECTION and operation == "create" and isinstance(commit.get("record"), dict):
        await mirror_repost(app, remote, rkey, commit["record"])
        await note_repost_seen(app, remote.did, rkey)
        await _react(app, remote, collection, rkey, commit["record"], REPOST_EMOJI)
        await nudge_notifications(app, commit["record"])
    elif collection == REPOST_COLLECTION and operation == "delete":
        await unmirror(app, f"at://{remote.did}/{REPOST_COLLECTION}/{rkey}")
        await unmirror_reaction(app, f"at://{remote.did}/{REPOST_COLLECTION}/{rkey}")
    elif collection == LIKE_COLLECTION and operation == "create" and isinstance(commit.get("record"), dict):
        await _react(app, remote, collection, rkey, commit["record"], LIKE_EMOJI)
        await nudge_notifications(app, commit["record"])
    elif collection == LIKE_COLLECTION and operation == "delete":
        await unmirror_reaction(app, f"at://{remote.did}/{LIKE_COLLECTION}/{rkey}")


async def catch_up_loop(app: FastAPI) -> None:
    interval = app.state.config.atproto.catchup_minutes * 60
    if interval <= 0:
        return
    # The first check comes soon after startup: whatever was posted while
    # the bridge was down may be beyond what the feed will replay.
    delay = _FIRST_CATCH_UP_DELAY
    rounds = 0
    while True:
        await asyncio.sleep(delay)
        delay = interval
        try:
            for remote in await app.state.repository.list_followed_accounts():
                # Every so often (and at startup) re-read the profile too:
                # a profile change the feed delivered before the saved
                # position, or not at all, would otherwise never arrive.
                if rounds % _PROFILE_REFRESH_EVERY == 0:
                    await _refresh(app, remote)
                else:
                    await catch_up(app, remote)
            rounds += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Catch-up check failed")


class JetstreamConsumer:
    def __init__(self, app: FastAPI, url: str) -> None:
        self._app = app
        self._url = url
        self._changed = asyncio.Event()
        self._connected_at: float | None = None
        self._events = 0

    def refresh(self) -> None:
        """Call when the set of followed accounts changes."""
        self._changed.set()

    async def _accounts(self) -> dict[str, RemoteAccount]:
        return {account.did: account for account in await self._app.state.repository.list_followed_accounts()}

    async def run(self) -> None:
        delay = _RECONNECT_DELAY
        while True:
            accounts = await self._accounts()
            if not accounts:
                await self._changed.wait()
                self._changed.clear()
                continue
            self._connected_at = None
            self._events = 0
            try:
                await self._consume(accounts)
                delay = _RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lasted = time.monotonic() - self._connected_at if self._connected_at is not None else None
                # The wait only grows while connections keep failing right
                # away. One that held for a while was a healthy connection
                # that happened to end, so start over from the short wait.
                if lasted is not None and lasted >= _STABLE_CONNECTION:
                    delay = _RECONNECT_DELAY
                if isinstance(exc, websockets.ConnectionClosed) and lasted is not None:
                    # Routine: the feed drops connections now and then. The
                    # saved position (and the catch-up check) covers the gap.
                    # How long it held, and how the server ended it, are
                    # logged to tell an occasional drop from a pattern.
                    close = exc.rcvd
                    how = f"close code {close.code} {close.reason!r}" if close is not None else "no close frame"
                    logger.info(
                        "Jetstream connection closed after %.0fs and %d event(s) (%s); reconnecting in %.0fs",
                        lasted, self._events, how, delay,
                    )
                else:
                    logger.warning("Jetstream connection failed (%r); reconnecting in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY)

    async def _send_options(self, socket: Any, accounts: dict[str, RemoteAccount]) -> None:
        await socket.send(
            json.dumps(
                {
                    "type": "options_update",
                    "payload": {
                        "wantedCollections": [POST_COLLECTION, REPOST_COLLECTION, LIKE_COLLECTION, PROFILE_COLLECTION],
                        "wantedDids": sorted(accounts),
                    },
                }
            )
        )

    async def _advance_cursor(self, time_us: int) -> None:
        repository = self._app.state.repository
        saved = await repository.get_value(_CURSOR_KEY)
        if saved is None or time_us > int(saved):
            await repository.set_value(_CURSOR_KEY, str(time_us))

    async def _consume(self, accounts: dict[str, RemoteAccount]) -> None:
        # requireHello: send nothing until told which accounts are wanted,
        # rather than the whole network in the meantime.
        url = f"{self._url}?requireHello=true"
        cursor = await self._app.state.repository.get_value(_CURSOR_KEY)
        if cursor:
            url += f"&cursor={max(int(cursor) - _CURSOR_REWIND_US, 0)}"
        async with websockets.connect(url, max_size=2_000_000) as socket:
            await self._send_options(socket, accounts)
            logger.info("Jetstream connected, watching %d account(s)", len(accounts))
            self._connected_at = time.monotonic()
            receive: asyncio.Task | None = None
            changed: asyncio.Task | None = None
            try:
                while True:
                    receive = receive or asyncio.create_task(socket.recv())
                    changed = changed or asyncio.create_task(self._changed.wait())
                    done, _ = await asyncio.wait(
                        {receive, changed}, return_when=asyncio.FIRST_COMPLETED, timeout=_IDLE_CURSOR_INTERVAL
                    )
                    if not done:
                        await self._advance_cursor(time.time_ns() // 1000 - _IDLE_CURSOR_MARGIN_US)
                        continue
                    if changed.done():
                        changed = None
                        self._changed.clear()
                        accounts = await self._accounts()
                        if not accounts:
                            return
                        await self._send_options(socket, accounts)
                    if receive.done():
                        message = receive.result()
                        receive = None
                        self._events += 1
                        try:
                            event = json.loads(message)
                        except ValueError:
                            continue
                        try:
                            await handle_event(self._app, event, accounts)
                        except Exception:
                            logger.exception("Error handling Jetstream event for %s", event.get("did"))
                        if isinstance(event.get("time_us"), int):
                            await self._advance_cursor(event["time_us"])
            finally:
                for task in (receive, changed):
                    if task is not None:
                        task.cancel()
