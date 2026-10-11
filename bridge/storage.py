"""Opening the repository ``storage.backend`` selects, and moving an
existing SQLite database into Postgres.

    python -m bridge.storage migrate [config.yaml] [path/to/bridge.db]

copies everything from the SQLite file (default: ``bridge.db`` under
``storage.data_dir``) into the Postgres database in ``storage.postgres``.
The config doesn't need ``backend: postgresql`` set yet -- run this with
the bridge stopped, then switch the backend and start it. The SQLite file
is only read, never changed, so it remains as a backup.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from bridge.config import BridgeConfig, ConfigError, load_config
from bridge.repository import BridgeRepository
from bridge.sqlite_repository import _SCHEMA, _STARTUP_FIXUPS, SqliteBridgeRepository


def sqlite_path(config: BridgeConfig) -> Path:
    return Path(config.storage.data_dir) / "bridge.db"


def open_repository(config: BridgeConfig) -> BridgeRepository:
    if config.storage.backend == "postgresql":
        # Imported here so SQLite deployments don't need psycopg2 at all.
        from bridge.postgres_repository import PostgresBridgeRepository

        return PostgresBridgeRepository(config.storage.postgres.resolved_dsn())
    return SqliteBridgeRepository(sqlite_path(config))


class MigrationError(Exception):
    pass


def migrate_sqlite_to_postgres(source: Path, dsn: str) -> dict[str, int]:
    """Copy every table of the SQLite database at ``source`` into the
    Postgres database at ``dsn``, in one transaction: either all of it
    arrives or none does. Refuses a target that already holds data.
    Returns the number of rows copied per table."""
    import psycopg2

    from bridge.postgres_repository import PostgresBridgeRepository

    if not source.is_file():
        raise MigrationError(f"No SQLite database at {source}")
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _SCHEMA)
    # Creates the schema (or brings an existing one up to date).
    PostgresBridgeRepository(dsn)._raw.close()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = psycopg2.connect(dsn)
    copied: dict[str, int] = {}
    try:
        cursor = dst.cursor()
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            if cursor.fetchone()[0]:
                raise MigrationError(f"The Postgres database already has data (table {table}); refusing to overwrite it.")
        for table in tables:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s",
                (table,),
            )
            target_columns = {row[0] for row in cursor.fetchall()}
            # A database from an older version may lack newer columns;
            # those take their defaults.
            columns = [row[1] for row in src.execute(f"PRAGMA table_info({table})") if row[1] in target_columns]
            if not columns:
                copied[table] = 0
                continue
            rows = src.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
            for row in rows:
                cursor.execute(statement, tuple(psycopg2.Binary(v) if isinstance(v, bytes) else v for v in row))
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            if cursor.fetchone()[0] != len(rows):
                raise MigrationError(f"Row count mismatch in {table} after copying")
            copied[table] = len(rows)
        # Firehose sequence numbers must carry on from where they were:
        # relays resume from the last one they saw.
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence('firehose_events', 'seq'), GREATEST((SELECT MAX(seq) FROM firehose_events), 1), "
            "(SELECT COUNT(*) FROM firehose_events) > 0)"
        )
        for statement in _STARTUP_FIXUPS:
            cursor.execute(statement)
        dst.commit()
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()
    return copied


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "migrate" or len(argv) > 3:
        print("Usage: python -m bridge.storage migrate [config.yaml] [path/to/bridge.db]", file=sys.stderr)
        return 2
    try:
        config = load_config(argv[1] if len(argv) > 1 else None)
        source = Path(argv[2]) if len(argv) > 2 else sqlite_path(config)
        copied = migrate_sqlite_to_postgres(source, config.storage.postgres.resolved_dsn())
    except (ConfigError, MigrationError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    for table, count in copied.items():
        print(f"  {table}: {count}")
    print(f"Copied {sum(copied.values())} rows from {source} into Postgres.")
    print("Now set storage.backend: postgresql in the config and start the bridge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
