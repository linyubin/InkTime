#!/usr/bin/env python3
"""Explicit first-install SQLite v2 initialization; never invoked on app startup.

Only an operator's --create command publishes a new empty database. The DB is
built and verified in a private same-directory file, then published using a
hard link (atomic create-if-absent on a local POSIX filesystem). Existing DBs,
including dangling symlinks, are never overwritten.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile

from db_schema import SCHEMA_VERSION, apply_migrations, verify_latest_schema


def initialize_database(database: Path) -> Path:
    """Create an empty, current-schema database without touching existing data.

    Requires an existing parent directory and a filesystem supporting hard
    links and directory fsync. Does not bootstrap photo records or auto-migrate.
    Concurrent initializers cannot overwrite one another's destination.
    """
    requested = Path(database).expanduser().absolute()
    parent = requested.parent.resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError(parent)
    target = parent / requested.name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing database: {target}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.init-", suffix=".db", dir=parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        with sqlite3.connect(temporary) as conn:
            # DELETE journal mode avoids leaving a WAL sidecar at publication.
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE photo_scores (path TEXT PRIMARY KEY)")
            before, after = apply_migrations(conn)
            if (before, after) != (0, SCHEMA_VERSION):
                raise RuntimeError("unexpected first-install migration version")
            conn.commit()
            verify_latest_schema(conn)
            if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("new database integrity_check failed")

        # Ensure the complete SQLite file is on disk before publication.
        read_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(read_fd)
        finally:
            os.close(read_fd)

        # os.link is an atomic no-replace operation, unlike os.replace.
        # EEXIST (including symlinks) or unsupported hardlinks fail closed.
        os.link(temporary, target)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(temp_name + suffix).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--create", action="store_true",
                        help="explicitly create a new empty SQLite v2 database")
    args = parser.parse_args()
    if not args.create:
        print(f"Dry run only: no database created. To initialize {args.database}, "
              "pass --create on an authorized staging/first-install host.")
        return
    path = initialize_database(args.database)
    print(f"Initialized empty SQLite v{SCHEMA_VERSION} database: {path}")


if __name__ == "__main__":
    main()
