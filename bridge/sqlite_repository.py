"""SQLite-backed ``BridgeRepository`` -- durable storage for bridge bookkeeping.

Persists to a single sqlite file under ``storage.data_dir``. This is the
default repository used by ``bridge.server.create_app``.

Every query here is written in the SQL that SQLite and PostgreSQL both
accept ("?" placeholders, ``ON CONFLICT`` rather than ``INSERT OR ...``,
``RETURNING`` rather than ``lastrowid``): ``bridge.postgres_repository``
runs these same methods against Postgres, so keep to that subset.

``sqlite3`` connections aren't safe to use concurrently from multiple
threads without care; this wraps every call in ``asyncio.to_thread`` behind
a single ``asyncio.Lock`` so the (fast, local, low-volume) database access
never blocks the event loop while still serializing access deterministically.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bridge.repository import (
    Account,
    BlobRow,
    ChatRoom,
    FirehoseEventMeta,
    HandleClaim,
    MirroredPost,
    PublishedMedia,
    RecordRow,
    RemoteAccount,
)

_SCHEMA = """
-- One row per Matrix user's hold on <label>.<bridge.domain>. Both columns
-- are unique: the PRIMARY KEY is what makes claiming a name atomic.
CREATE TABLE IF NOT EXISTS handle_claims (
    label TEXT PRIMARY KEY,
    matrix_user_id TEXT NOT NULL UNIQUE,
    did TEXT UNIQUE,
    claimed_at TEXT NOT NULL
);

-- Keys belonging to the bridge itself rather than to one account, e.g.
-- 'plc_rotation' (the key that controls every local account's did:plc).
CREATE TABLE IF NOT EXISTS server_keys (
    name TEXT PRIMARY KEY,
    private_hex TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    did TEXT PRIMARY KEY,
    matrix_user_id TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL UNIQUE,
    signing_key_hex TEXT NOT NULL,
    genesis_operation TEXT NOT NULL,
    plc_registered INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    rev TEXT NOT NULL,
    commit_cid TEXT NOT NULL,
    commit_block BLOB NOT NULL,
    data_cid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    profile_room_id TEXT UNIQUE
);

-- The repository index: which records exist and what they hash to. No
-- record content -- `source` says how to rebuild it from Matrix.
CREATE TABLE IF NOT EXISTS records (
    did TEXT NOT NULL,
    collection TEXT NOT NULL,
    rkey TEXT NOT NULL,
    cid TEXT NOT NULL,
    source TEXT NOT NULL,
    matrix_event_id TEXT,
    PRIMARY KEY (did, collection, rkey)
);

-- Sequence numbers handed out on the firehose, and which account each was
-- about. Payloads are not stored (they contain record content).
-- AUTOINCREMENT so a number is never reused, even after rows are pruned.
CREATE TABLE IF NOT EXISTS firehose_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    did TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Where each blob's bytes are in the Matrix media repository.
CREATE TABLE IF NOT EXISTS blobs (
    did TEXT NOT NULL,
    cid TEXT NOT NULL,
    mxc TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    matrix_event_id TEXT,
    PRIMARY KEY (did, cid)
);

-- Matrix media the bridge serves publicly at /media/...
CREATE TABLE IF NOT EXISTS published_media (
    mxc TEXT PRIMARY KEY,
    matrix_event_id TEXT
);

CREATE TABLE IF NOT EXISTS remote_accounts (
    did TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    pds TEXT NOT NULL,
    ghost_user_id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL UNIQUE
);

-- Which local account follows which remote one, and the record key of the
-- app.bsky.graph.follow record that says so (needed to delete it).
CREATE TABLE IF NOT EXISTS follows (
    local_did TEXT NOT NULL,
    remote_did TEXT NOT NULL,
    rkey TEXT NOT NULL,
    PRIMARY KEY (local_did, remote_did)
);

CREATE TABLE IF NOT EXISTS mirrored_posts (
    uri TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_ids TEXT NOT NULL,
    cid TEXT NOT NULL DEFAULT '',
    root_uri TEXT NOT NULL DEFAULT '',
    root_cid TEXT NOT NULL DEFAULT ''
);

-- Which post each mirrored Matrix event shows: what a reaction or reply
-- to that event acts on.
CREATE TABLE IF NOT EXISTS mirrored_events (
    event_id TEXT PRIMARY KEY,
    uri TEXT NOT NULL,
    cid TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bridge_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The space and notifications room the bridge keeps for each Matrix user.
CREATE TABLE IF NOT EXISTS user_rooms (
    matrix_user_id TEXT PRIMARY KEY,
    space_room_id TEXT,
    notifications_room_id TEXT
);

CREATE TABLE IF NOT EXISTS chat_rooms (
    room_id TEXT PRIMARY KEY,
    local_did TEXT NOT NULL,
    remote_did TEXT NOT NULL,
    convo_id TEXT NOT NULL,
    UNIQUE (local_did, remote_did)
);

-- Which Matrix event each chat message is, in both directions: so a
-- message the bridge sent isn't mirrored back when the chat service
-- reports it, and a redaction can find what to delete.
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_id TEXT NOT NULL
);

-- Matrix events already handled, so one Synapse sends again isn't handled
-- twice. Kept by event ID, not transaction ID: Synapse on SQLite numbers
-- its transactions from 1 again after a restart. Forgotten after a week.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    processed_at INTEGER NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition).
# CREATE TABLE IF NOT EXISTS leaves an existing table as it was, so these
# are added to it on startup. (SQLite can't add a UNIQUE column in place;
# the indexes below provide that for databases upgraded this way.)
_ADDED_COLUMNS = (
    ("accounts", "profile_room_id", "TEXT"),
    ("records", "matrix_event_id", "TEXT"),
    ("mirrored_posts", "root_uri", "TEXT NOT NULL DEFAULT ''"),
    ("mirrored_posts", "root_cid", "TEXT NOT NULL DEFAULT ''"),
    ("mirrored_events", "cid", "TEXT NOT NULL DEFAULT ''"),
)

_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS accounts_profile_room ON accounts (profile_room_id);
CREATE INDEX IF NOT EXISTS records_matrix_event ON records (matrix_event_id);
CREATE INDEX IF NOT EXISTS processed_events_at ON processed_events (processed_at);
"""

# Run on every startup, after the schema is in place.
_STARTUP_FIXUPS = (
    # Events mirrored before mirrored_events recorded a post's hash take it
    # from the post they belong to.
    "UPDATE mirrored_events SET cid = COALESCE((SELECT cid FROM mirrored_posts WHERE uri = mirrored_events.uri), '') "
    "WHERE cid = ''",
    # Replaced by processed_events.
    "DROP TABLE IF EXISTS processed_transactions",
)

# How long a handled Matrix event is remembered. Synapse gives up retrying
# a transaction well before this.
_PROCESSED_EVENT_RETENTION_SECONDS = 7 * 24 * 3600

_CLAIM_COLUMNS = "label, matrix_user_id, did, claimed_at"
_ACCOUNT_COLUMNS = (
    "did, matrix_user_id, label, signing_key_hex, genesis_operation, plc_registered, active, "
    "rev, commit_cid, commit_block, data_cid, created_at, profile_room_id"
)
_RECORD_COLUMNS = "did, collection, rkey, cid, source, matrix_event_id"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account(row: tuple) -> Account:
    return Account(
        did=row[0],
        matrix_user_id=row[1],
        label=row[2],
        signing_key_hex=row[3],
        genesis_operation=row[4],
        plc_registered=bool(row[5]),
        active=bool(row[6]),
        rev=row[7],
        commit_cid=row[8],
        commit_block=bytes(row[9]),
        data_cid=row[10],
        created_at=row[11],
        profile_room_id=row[12],
    )


class SqliteBridgeRepository:
    def __init__(self, path: str | Path) -> None:
        """``path`` may be ``":memory:"`` for a throwaway database."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        for table, column, definition in _ADDED_COLUMNS:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._conn.executescript(_INDEXES)
        for statement in _STARTUP_FIXUPS:
            self._conn.execute(statement)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run ``fn`` in a worker thread inside one transaction: committed
        if it returns, rolled back if it raises."""

        def transaction(conn: sqlite3.Connection) -> Any:
            with conn:
                return fn(conn)

        async with self._lock:
            return await asyncio.to_thread(transaction, self._conn)

    # -- handles ------------------------------------------------------------

    async def get_handle_claim(self, label: str) -> HandleClaim | None:
        def query(conn: sqlite3.Connection) -> HandleClaim | None:
            row = conn.execute(f"SELECT {_CLAIM_COLUMNS} FROM handle_claims WHERE label = ?", (label,)).fetchone()
            return HandleClaim(*row) if row else None

        return await self._run(query)

    async def get_handle_claim_for_user(self, matrix_user_id: str) -> HandleClaim | None:
        def query(conn: sqlite3.Connection) -> HandleClaim | None:
            row = conn.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM handle_claims WHERE matrix_user_id = ?", (matrix_user_id,)
            ).fetchone()
            return HandleClaim(*row) if row else None

        return await self._run(query)

    async def list_handle_claims(self) -> list[HandleClaim]:
        def query(conn: sqlite3.Connection) -> list[HandleClaim]:
            rows = conn.execute(f"SELECT {_CLAIM_COLUMNS} FROM handle_claims ORDER BY label").fetchall()
            return [HandleClaim(*row) for row in rows]

        return await self._run(query)

    async def claim_handle(self, label: str, matrix_user_id: str) -> HandleClaim | None:
        claim = HandleClaim(label=label, matrix_user_id=matrix_user_id, did=None, claimed_at=_now())

        def insert(conn: sqlite3.Connection) -> HandleClaim | None:
            # OR IGNORE covers both unique constraints: the label being
            # held, and this user already holding a different label.
            cursor = conn.execute(
                "INSERT INTO handle_claims (label, matrix_user_id, did, claimed_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (claim.label, claim.matrix_user_id, claim.did, claim.claimed_at),
            )
            return claim if cursor.rowcount == 1 else None

        return await self._run(insert)

    async def set_handle_did(self, label: str, did: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE handle_claims SET did = ? WHERE label = ?", (did, label)))

    async def release_handle(self, label: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM handle_claims WHERE label = ?", (label,)))

    # -- server keys --------------------------------------------------------

    async def get_or_create_server_key(self, name: str, candidate_hex: str) -> str:
        def upsert(conn: sqlite3.Connection) -> str:
            conn.execute("INSERT INTO server_keys (name, private_hex) VALUES (?, ?) ON CONFLICT DO NOTHING", (name, candidate_hex))
            return conn.execute("SELECT private_hex FROM server_keys WHERE name = ?", (name,)).fetchone()[0]

        return await self._run(upsert)

    # -- accounts -----------------------------------------------------------

    async def create_account(self, account: Account) -> None:
        def insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO accounts ({_ACCOUNT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account.did,
                    account.matrix_user_id,
                    account.label,
                    account.signing_key_hex,
                    account.genesis_operation,
                    int(account.plc_registered),
                    int(account.active),
                    account.rev,
                    account.commit_cid,
                    account.commit_block,
                    account.data_cid,
                    account.created_at,
                    account.profile_room_id,
                ),
            )

        await self._run(insert)

    async def _get_account_where(self, column: str, value: str) -> Account | None:
        def query(conn: sqlite3.Connection) -> Account | None:
            row = conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE {column} = ?", (value,)).fetchone()
            return _account(row) if row else None

        return await self._run(query)

    async def get_account(self, did: str) -> Account | None:
        return await self._get_account_where("did", did)

    async def get_account_for_user(self, matrix_user_id: str) -> Account | None:
        return await self._get_account_where("matrix_user_id", matrix_user_id)

    async def get_account_for_label(self, label: str) -> Account | None:
        return await self._get_account_where("label", label)

    async def list_accounts(self) -> list[Account]:
        def query(conn: sqlite3.Connection) -> list[Account]:
            rows = conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts ORDER BY did").fetchall()
            return [_account(row) for row in rows]

        return await self._run(query)

    async def mark_plc_registered(self, did: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE accounts SET plc_registered = 1 WHERE did = ?", (did,)))

    async def set_profile_room(self, did: str, room_id: str) -> None:
        await self._run(
            lambda conn: conn.execute("UPDATE accounts SET profile_room_id = ? WHERE did = ?", (room_id, did))
        )

    async def get_account_for_profile_room(self, room_id: str) -> Account | None:
        return await self._get_account_where("profile_room_id", room_id)

    async def clear_profile_room(self, did: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE accounts SET profile_room_id = NULL WHERE did = ?", (did,)))

    async def delete_account(self, did: str) -> None:
        def delete(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT matrix_user_id, label FROM accounts WHERE did = ?", (did,)).fetchone()
            if row is None:
                return
            matrix_user_id, label = row
            for table, column in (("records", "did"), ("blobs", "did"), ("follows", "local_did"), ("chat_rooms", "local_did")):
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (did,))
            conn.execute("DELETE FROM bridge_state WHERE key LIKE ?", (f"%:{did}",))
            conn.execute("DELETE FROM handle_claims WHERE label = ?", (label,))
            conn.execute("DELETE FROM user_rooms WHERE matrix_user_id = ?", (matrix_user_id,))
            conn.execute("DELETE FROM accounts WHERE did = ?", (did,))

        await self._run(delete)

    # -- records ------------------------------------------------------------

    async def list_records(self, did: str) -> list[RecordRow]:
        def query(conn: sqlite3.Connection) -> list[RecordRow]:
            rows = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM records WHERE did = ? ORDER BY collection, rkey", (did,)
            ).fetchall()
            return [RecordRow(*row) for row in rows]

        return await self._run(query)

    async def get_record(self, did: str, collection: str, rkey: str) -> RecordRow | None:
        def query(conn: sqlite3.Connection) -> RecordRow | None:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM records WHERE did = ? AND collection = ? AND rkey = ?",
                (did, collection, rkey),
            ).fetchone()
            return RecordRow(*row) if row else None

        return await self._run(query)

    async def get_records_for_event(self, matrix_event_id: str) -> list[RecordRow]:
        def query(conn: sqlite3.Connection) -> list[RecordRow]:
            rows = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM records WHERE matrix_event_id = ? ORDER BY collection, rkey",
                (matrix_event_id,),
            ).fetchall()
            return [RecordRow(*row) for row in rows]

        return await self._run(query)

    # -- media --------------------------------------------------------------

    async def add_blob(self, blob: BlobRow) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO blobs (did, cid, mxc, mime_type, size, matrix_event_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (did, cid) DO UPDATE SET mxc = excluded.mxc, mime_type = excluded.mime_type, size = excluded.size, matrix_event_id = excluded.matrix_event_id",
                (blob.did, blob.cid, blob.mxc, blob.mime_type, blob.size, blob.matrix_event_id),
            )
        )

    async def get_blob(self, did: str, cid: str) -> BlobRow | None:
        def query(conn: sqlite3.Connection) -> BlobRow | None:
            row = conn.execute(
                "SELECT did, cid, mxc, mime_type, size, matrix_event_id FROM blobs WHERE did = ? AND cid = ?", (did, cid)
            ).fetchone()
            return BlobRow(*row) if row else None

        return await self._run(query)

    async def list_blobs(self, did: str) -> list[BlobRow]:
        def query(conn: sqlite3.Connection) -> list[BlobRow]:
            rows = conn.execute(
                "SELECT did, cid, mxc, mime_type, size, matrix_event_id FROM blobs WHERE did = ? ORDER BY cid", (did,)
            ).fetchall()
            return [BlobRow(*row) for row in rows]

        return await self._run(query)

    async def publish_media(self, media: PublishedMedia) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO published_media (mxc, matrix_event_id) VALUES (?, ?) "
                "ON CONFLICT (mxc) DO UPDATE SET matrix_event_id = excluded.matrix_event_id",
                (media.mxc, media.matrix_event_id),
            )
        )

    async def get_published_media(self, mxc: str) -> PublishedMedia | None:
        def query(conn: sqlite3.Connection) -> PublishedMedia | None:
            row = conn.execute(
                "SELECT mxc, matrix_event_id FROM published_media WHERE mxc = ?", (mxc,)
            ).fetchone()
            return PublishedMedia(*row) if row else None

        return await self._run(query)

    async def forget_media_for_event(self, matrix_event_id: str) -> None:
        def delete(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM blobs WHERE matrix_event_id = ?", (matrix_event_id,))
            conn.execute("DELETE FROM published_media WHERE matrix_event_id = ?", (matrix_event_id,))

        await self._run(delete)

    async def commit_repo(
        self,
        did: str,
        *,
        rev: str,
        commit_cid: str,
        commit_block: bytes,
        data_cid: str,
        upserts: list[RecordRow],
        deletes: list[tuple[str, str]],
    ) -> None:
        def apply(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "DELETE FROM records WHERE did = ? AND collection = ? AND rkey = ?",
                [(did, collection, rkey) for collection, rkey in deletes],
            )
            conn.executemany(
                f"INSERT INTO records ({_RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT (did, collection, rkey) DO UPDATE SET cid = excluded.cid, source = excluded.source, matrix_event_id = excluded.matrix_event_id",
                [(row.did, row.collection, row.rkey, row.cid, row.source, row.matrix_event_id) for row in upserts],
            )
            conn.execute(
                "UPDATE accounts SET rev = ?, commit_cid = ?, commit_block = ?, data_cid = ? WHERE did = ?",
                (rev, commit_cid, commit_block, data_cid, did),
            )

        await self._run(apply)

    # -- firehose -----------------------------------------------------------

    async def next_firehose_seq(self, did: str, kind: str) -> int:
        def insert(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO firehose_events (did, kind, created_at) VALUES (?, ?, ?) RETURNING seq", (did, kind, _now())
            )
            return int(cursor.fetchone()[0])

        return await self._run(insert)

    async def current_firehose_seq(self) -> int:
        def query(conn: sqlite3.Connection) -> int:
            return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM firehose_events").fetchone()[0]

        return await self._run(query)

    async def firehose_events_between(self, after: int, upto: int) -> list[FirehoseEventMeta]:
        def query(conn: sqlite3.Connection) -> list[FirehoseEventMeta]:
            rows = conn.execute(
                "SELECT seq, did, kind FROM firehose_events WHERE seq > ? AND seq <= ? ORDER BY seq", (after, upto)
            ).fetchall()
            return [FirehoseEventMeta(*row) for row in rows]

        return await self._run(query)

    # -- remote accounts ----------------------------------------------------

    async def save_remote_account(self, account: RemoteAccount) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO remote_accounts (did, handle, pds, ghost_user_id, room_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (did) DO UPDATE SET handle = excluded.handle, pds = excluded.pds, ghost_user_id = excluded.ghost_user_id, room_id = excluded.room_id",
                (account.did, account.handle, account.pds, account.ghost_user_id, account.room_id),
            )
        )

    async def _get_remote_account_where(self, column: str, value: str) -> RemoteAccount | None:
        def query(conn: sqlite3.Connection) -> RemoteAccount | None:
            row = conn.execute(
                f"SELECT did, handle, pds, ghost_user_id, room_id FROM remote_accounts WHERE {column} = ?", (value,)
            ).fetchone()
            return RemoteAccount(*row) if row else None

        return await self._run(query)

    async def get_remote_account(self, did: str) -> RemoteAccount | None:
        return await self._get_remote_account_where("did", did)

    async def get_remote_account_for_room(self, room_id: str) -> RemoteAccount | None:
        return await self._get_remote_account_where("room_id", room_id)

    async def list_remote_accounts(self) -> list[RemoteAccount]:
        def query(conn: sqlite3.Connection) -> list[RemoteAccount]:
            rows = conn.execute(
                "SELECT did, handle, pds, ghost_user_id, room_id FROM remote_accounts ORDER BY handle"
            ).fetchall()
            return [RemoteAccount(*row) for row in rows]

        return await self._run(query)

    async def add_follow(self, local_did: str, remote_did: str, rkey: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO follows (local_did, remote_did, rkey) VALUES (?, ?, ?) "
                "ON CONFLICT (local_did, remote_did) DO UPDATE SET rkey = excluded.rkey",
                (local_did, remote_did, rkey),
            )
        )

    async def get_follow_rkey(self, local_did: str, remote_did: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT rkey FROM follows WHERE local_did = ? AND remote_did = ?", (local_did, remote_did)
            ).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def remove_follow(self, local_did: str, remote_did: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "DELETE FROM follows WHERE local_did = ? AND remote_did = ?", (local_did, remote_did)
            )
        )

    async def _list_remote_accounts(self, where: str, params: tuple) -> list[RemoteAccount]:
        def query(conn: sqlite3.Connection) -> list[RemoteAccount]:
            rows = conn.execute(
                "SELECT DISTINCT r.did, r.handle, r.pds, r.ghost_user_id, r.room_id FROM remote_accounts r "
                f"JOIN follows f ON f.remote_did = r.did {where} ORDER BY r.handle",
                params,
            ).fetchall()
            return [RemoteAccount(*row) for row in rows]

        return await self._run(query)

    async def list_follows(self, local_did: str) -> list[RemoteAccount]:
        return await self._list_remote_accounts("WHERE f.local_did = ?", (local_did,))

    async def list_followed_accounts(self) -> list[RemoteAccount]:
        return await self._list_remote_accounts("", ())

    async def add_mirrored_post(self, post: MirroredPost) -> bool:
        def insert(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "INSERT INTO mirrored_posts (uri, room_id, event_ids, cid, root_uri, root_cid) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (post.uri, post.room_id, json.dumps(post.event_ids), post.cid, post.root_uri, post.root_cid),
            )
            if cursor.rowcount != 1:
                return False
            conn.executemany(
                "INSERT INTO mirrored_events (event_id, uri, cid) VALUES (?, ?, ?) "
                "ON CONFLICT (event_id) DO UPDATE SET uri = excluded.uri, cid = excluded.cid",
                [(event_id, post.shown_uri or post.uri, post.shown_cid or post.cid) for event_id in post.event_ids],
            )
            return True

        return await self._run(insert)

    async def get_mirrored_post(self, uri: str) -> MirroredPost | None:
        def query(conn: sqlite3.Connection) -> MirroredPost | None:
            row = conn.execute(
                "SELECT uri, room_id, event_ids, cid, root_uri, root_cid FROM mirrored_posts WHERE uri = ?", (uri,)
            ).fetchone()
            return MirroredPost(row[0], row[1], json.loads(row[2]), row[3], row[4], row[5]) if row else None

        return await self._run(query)

    async def get_shown_post_for_event(self, event_id: str) -> tuple[str, str] | None:
        def query(conn: sqlite3.Connection) -> tuple[str, str] | None:
            row = conn.execute("SELECT uri, cid FROM mirrored_events WHERE event_id = ?", (event_id,)).fetchone()
            return (row[0], row[1]) if row else None

        return await self._run(query)

    async def get_mirrored_post_for_event(self, event_id: str) -> MirroredPost | None:
        shown = await self.get_shown_post_for_event(event_id)
        return await self.get_mirrored_post(shown[0]) if shown else None

    async def delete_mirrored_post(self, uri: str) -> None:
        def delete(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT event_ids FROM mirrored_posts WHERE uri = ?", (uri,)).fetchone()
            conn.execute("DELETE FROM mirrored_posts WHERE uri = ?", (uri,))
            for event_id in json.loads(row[0]) if row else []:
                conn.execute("DELETE FROM mirrored_events WHERE event_id = ?", (event_id,))

        await self._run(delete)

    async def get_value(self, key: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT value FROM bridge_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def set_value(self, key: str, value: str) -> None:
        await self._run(
            lambda conn: conn.execute("INSERT INTO bridge_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        )

    async def delete_value(self, key: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM bridge_state WHERE key = ?", (key,)))

    # -- per-user rooms -----------------------------------------------------

    async def _get_user_room(self, column: str, matrix_user_id: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(f"SELECT {column} FROM user_rooms WHERE matrix_user_id = ?", (matrix_user_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def _set_user_room(self, column: str, matrix_user_id: str, room_id: str) -> None:
        def upsert(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO user_rooms (matrix_user_id) VALUES (?) ON CONFLICT DO NOTHING", (matrix_user_id,))
            conn.execute(f"UPDATE user_rooms SET {column} = ? WHERE matrix_user_id = ?", (room_id, matrix_user_id))

        await self._run(upsert)

    async def get_user_space(self, matrix_user_id: str) -> str | None:
        return await self._get_user_room("space_room_id", matrix_user_id)

    async def set_user_space(self, matrix_user_id: str, room_id: str) -> None:
        await self._set_user_room("space_room_id", matrix_user_id, room_id)

    async def get_notifications_room(self, matrix_user_id: str) -> str | None:
        return await self._get_user_room("notifications_room_id", matrix_user_id)

    async def set_notifications_room(self, matrix_user_id: str, room_id: str) -> None:
        await self._set_user_room("notifications_room_id", matrix_user_id, room_id)

    async def get_notifications_room_owner(self, room_id: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT matrix_user_id FROM user_rooms WHERE notifications_room_id = ?", (room_id,)
            ).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_space_owner(self, room_id: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT matrix_user_id FROM user_rooms WHERE space_room_id = ?", (room_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    # -- chats --------------------------------------------------------------

    async def save_chat_room(self, chat: ChatRoom) -> None:
        def upsert(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM chat_rooms WHERE local_did = ? AND remote_did = ?", (chat.local_did, chat.remote_did))
            conn.execute(
                "INSERT INTO chat_rooms (room_id, local_did, remote_did, convo_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (room_id) DO UPDATE SET local_did = excluded.local_did, remote_did = excluded.remote_did, convo_id = excluded.convo_id",
                (chat.room_id, chat.local_did, chat.remote_did, chat.convo_id),
            )

        await self._run(upsert)

    async def _get_chat_room_where(self, where: str, params: tuple) -> ChatRoom | None:
        def query(conn: sqlite3.Connection) -> ChatRoom | None:
            row = conn.execute(
                f"SELECT room_id, local_did, remote_did, convo_id FROM chat_rooms WHERE {where}", params
            ).fetchone()
            return ChatRoom(*row) if row else None

        return await self._run(query)

    async def get_chat_room(self, local_did: str, remote_did: str) -> ChatRoom | None:
        return await self._get_chat_room_where("local_did = ? AND remote_did = ?", (local_did, remote_did))

    async def get_chat_room_for_convo(self, local_did: str, convo_id: str) -> ChatRoom | None:
        return await self._get_chat_room_where("local_did = ? AND convo_id = ?", (local_did, convo_id))

    async def get_chat_room_for_room(self, room_id: str) -> ChatRoom | None:
        return await self._get_chat_room_where("room_id = ?", (room_id,))

    async def list_chat_rooms(self, local_did: str) -> list[ChatRoom]:
        def query(conn: sqlite3.Connection) -> list[ChatRoom]:
            rows = conn.execute(
                "SELECT room_id, local_did, remote_did, convo_id FROM chat_rooms WHERE local_did = ?", (local_did,)
            ).fetchall()
            return [ChatRoom(*row) for row in rows]

        return await self._run(query)

    async def add_chat_message(self, message_id: str, room_id: str, event_id: str) -> bool:
        def insert(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "INSERT INTO chat_messages (message_id, room_id, event_id) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (message_id, room_id, event_id),
            )
            return cursor.rowcount == 1

        return await self._run(insert)

    async def get_chat_message_for_event(self, event_id: str) -> str | None:
        def query(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT message_id FROM chat_messages WHERE event_id = ?", (event_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_event_for_chat_message(self, message_id: str) -> tuple[str, str] | None:
        def query(conn: sqlite3.Connection) -> tuple[str, str] | None:
            row = conn.execute("SELECT room_id, event_id FROM chat_messages WHERE message_id = ?", (message_id,)).fetchone()
            return (row[0], row[1]) if row else None

        return await self._run(query)

    # -- Matrix AppService --------------------------------------------------

    async def has_processed_event(self, event_id: str) -> bool:
        def query(conn: sqlite3.Connection) -> bool:
            return conn.execute("SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)).fetchone() is not None

        return await self._run(query)

    async def mark_event_processed(self, event_id: str) -> None:
        now = int(time.time())

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?) ON CONFLICT DO NOTHING", (event_id, now)
            )
            conn.execute("DELETE FROM processed_events WHERE processed_at < ?", (now - _PROCESSED_EVENT_RETENTION_SECONDS,))

        await self._run(write)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)
