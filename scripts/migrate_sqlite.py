#!/usr/bin/env python3
"""Inspect or explicitly migrate an EXISTING InkTime SQLite DB (never auto-run).

Examples:
  python -m scripts.migrate_sqlite --database staging/photos.db
  python -m scripts.migrate_sqlite --database staging/photos.db --backup-dir staging/backups --apply

Stop application writers first. A verified backup is retained even if a migration
fails; no source DB or backup is ever overwritten by the rollback procedure.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from db_schema import (
    SCHEMA_VERSION, MigrationError, _require_photo_table,
    _verify_photo_columns, apply_migrations, current_version,
    verify_latest_schema,
)
from scripts.backup_sqlite import backup_database, restore_drill


@dataclass(frozen=True)
class MigrationResult:
    previous_version: int
    version: int
    backup_path: Path | None


def _source_path(database: Path) -> Path:
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"existing SQLite database required: {database}")
    return database


def inspect_database(database: Path) -> int:
    """Read-only schema preflight. Never creates a missing SQLite file."""
    source = _source_path(database)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
        version = current_version(conn)
        _require_photo_table(conn)
        if version >= 1:
            _verify_photo_columns(conn)
        if version == SCHEMA_VERSION:
            verify_latest_schema(conn)
        return version


def migrate_database(
    database: Path, backup_dir: Path, *, busy_timeout_ms: int = 5000
) -> MigrationResult:
    """Back up and migrate while holding BEGIN IMMEDIATE across both steps.

    No migration occurs without a verified, isolated backup. Concurrent SQLite
    writers are excluded by the transaction; other app processes should still
    be stopped for the full maintenance window.
    """
    source = _source_path(database)
    if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 60000:
        raise ValueError("busy_timeout_ms must be 1..60000")
    backup_dir = Path(backup_dir).expanduser().resolve()
    with closing(sqlite3.connect(
        source.as_uri() + "?mode=rw", uri=True,
        isolation_level=None, timeout=busy_timeout_ms / 1000,
    )) as conn:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = current_version(conn)
            _require_photo_table(conn)
            if before >= 1:
                _verify_photo_columns(conn)
            if before == SCHEMA_VERSION:
                verify_latest_schema(conn)
                conn.execute("ROLLBACK")
                return MigrationResult(before, before, None)

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_dir / f"{source.stem}-pre-v{before}-{stamp}-{uuid4().hex[:8]}.db"
            backup_database(source, backup)
            # A backup that cannot be independently restored is not sufficient.
            with TemporaryDirectory(prefix="inktime-migration-restore-") as temp:
                restore_drill(backup, Path(temp) / "restored.db")

            original, new = apply_migrations(conn)
            conn.execute("COMMIT")
            return MigrationResult(original, new, backup)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="explicitly authorize a backed-up schema migration")
    args = parser.parse_args()
    if not args.apply:
        version = inspect_database(args.database)
        print(f"Read-only schema check: v{version}; supported v{SCHEMA_VERSION}; "
              f"{'up to date' if version == SCHEMA_VERSION else 'migration available'}")
        return
    if args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    result = migrate_database(args.database, args.backup_dir)
    if result.backup_path is None:
        print(f"Schema already at v{result.version}; no changes or backup needed")
    else:
        print(f"Migrated v{result.previous_version} -> v{result.version}; "
              f"verified backup retained: {result.backup_path}")


if __name__ == "__main__":
    main()
