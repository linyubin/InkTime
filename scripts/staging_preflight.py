#!/usr/bin/env python3
"""R1 staging-only preflight: inspect an existing database and paths.

This tool intentionally never imports config.py or initializes a database.
It does not create directories, migrate schemas, run web servers or contact
devices. Success is not a production/NAS or operator acceptance sign-off.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from db_schema import MigrationError, open_current_readonly_database


class PreflightError(RuntimeError):
    """Staging environment does not meet a prerequisite."""


def inspect_staging(database: Path, images: Path, output: Path) -> None:
    """Fail closed on absent paths, unsupported schema or integrity failure.

    Does not test network disconnects, permissions for writes, directory fsync,
    HTTPS, or actual photo scan. The operator must use an isolated staging DB.
    """
    db = Path(database).expanduser()
    source = Path(images).expanduser()
    target = Path(output).expanduser()
    if not db.is_file():
        raise PreflightError(f"database file does not exist: {db}")
    if not source.is_dir():
        raise PreflightError(f"image directory does not exist: {source}")
    if not target.is_dir():
        raise PreflightError(f"output directory does not exist: {target}")
    try:
        with closing(open_current_readonly_database(db)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise PreflightError("SQLite integrity_check failed")
    except (MigrationError, sqlite3.DatabaseError, OSError) as exc:
        raise PreflightError(f"SQLite preflight failed: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only staging prerequisites check; no acceptance sign-off")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        inspect_staging(args.database, args.images, args.output)
    except PreflightError as exc:
        parser.exit(1, f"Staging preflight BLOCKED: {exc}\n")
    print("Staging preflight OK: SQLite v2/integrity and directories; NAS/HTTPS/device NOT verified")


if __name__ == "__main__":
    main()
