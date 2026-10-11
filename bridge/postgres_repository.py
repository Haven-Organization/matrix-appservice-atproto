"""PostgreSQL-backed ``BridgeRepository``.

The same bookkeeping as ``bridge.sqlite_repository``, in a Postgres
database instead of a file -- selected with ``storage.backend: postgresql``.
It *is* the SQLite repository with a different connection underneath: every
query lives once, in ``SqliteBridgeRepository``, written in the SQL both
databases accept, so the two backends can't drift apart. What differs is
only what this module supplies: the connection, the placeholder style, and
the column types of the schema.

Access is serialized the same way too: one connection, used from a worker
thread behind a single ``asyncio.Lock``. The bridge's database traffic is
light and local, and one connection keeps each operation's transaction
semantics identical to SQLite's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from typing import Any, Callable, Iterable

import psycopg2

from bridge.sqlite_repository import _ADDED_COLUMNS, _INDEXES, _SCHEMA, _STARTUP_FIXUPS, SqliteBridgeRepository

logger = logging.getLogger(__name__)

_STARTUP_TIMEOUT = 60.0


def postgres_schema() -> str:
    """The SQLite schema in Postgres's types.

    Text columns get the "C" collation: SQLite compares text byte by byte,
    and record keys and collection names have to sort the same way here
    (a locale-aware collation ignores the dots in "app.bsky.feed.post").
    """
    return (
        _SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        .replace(" BLOB", " BYTEA")
        .replace(" INTEGER", " BIGINT")
        .replace(" TEXT", ' TEXT COLLATE "C"')
    )


@lru_cache(maxsize=512)
def _translate(sql: str) -> str:
    """``?`` placeholders to psycopg2's ``%s`` (escaping any literal ``%``)."""
    return sql.replace("%", "%%").replace("?", "%s")


class _Connection:
    """The slice of ``sqlite3.Connection`` the shared queries use, over a
    psycopg2 connection."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        cursor = self._raw.cursor()
        params = tuple(params)
        if params:
            cursor.execute(_translate(sql), params)
        else:
            cursor.execute(sql)
        return cursor

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> Any:
        cursor = self._raw.cursor()
        translated = _translate(sql)
        for params in rows:
            cursor.execute(translated, tuple(params))
        return cursor


class PostgresBridgeRepository(SqliteBridgeRepository):
    def __init__(self, dsn: str) -> None:
        """``dsn`` is a libpq connection string or ``postgresql://`` URL."""
        self._dsn = dsn
        self._raw: Any = None
        self._lock = asyncio.Lock()
        conn = self._connect_at_startup()
        try:
            cursor = conn.cursor()
            cursor.execute(postgres_schema())
            for table, column, definition in _ADDED_COLUMNS:
                definition = definition.replace("TEXT", 'TEXT COLLATE "C"')
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            cursor.execute(_INDEXES)
            for statement in _STARTUP_FIXUPS:
                cursor.execute(statement)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _connect_at_startup(self) -> Any:
        """The database may still be starting when the bridge does (both
        brought up together at boot, or by compose), so give it a while."""
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while True:
            try:
                return self._connection()
            except psycopg2.OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise
                logger.info("Waiting for the database: %s", str(exc).strip().splitlines()[0])
                time.sleep(2)

    def _connection(self) -> Any:
        if self._raw is None or self._raw.closed:
            self._raw = psycopg2.connect(self._dsn)
        return self._raw

    async def _run(self, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn`` in a worker thread inside one transaction: committed
        if it returns, rolled back if it raises. If the connection turns
        out to have been lost (the database was restarted, say), reconnect
        and run it once more."""

        def transaction() -> Any:
            for attempt in (1, 2):
                conn = self._connection()
                try:
                    result = fn(_Connection(conn))
                    conn.commit()
                    return result
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    lost = bool(conn.closed)
                    self._discard(conn, lost)
                    if lost and attempt == 1:
                        logger.warning("Lost the database connection; reconnecting")
                        continue
                    raise
                except BaseException:
                    self._discard(conn, bool(conn.closed))
                    raise

        async with self._lock:
            return await asyncio.to_thread(transaction)

    def _discard(self, conn: Any, lost: bool) -> None:
        try:
            if lost:
                self._raw = None
            else:
                conn.rollback()
        except psycopg2.Error:
            self._raw = None

    async def close(self) -> None:
        async with self._lock:
            if self._raw is not None and not self._raw.closed:
                await asyncio.to_thread(self._raw.close)
            self._raw = None
