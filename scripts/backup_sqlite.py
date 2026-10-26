#!/usr/bin/env python3
"""Create and verify a consistent SQLite backup (including committed WAL data)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def backup_database(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source not found: {source}")
    if source == destination:
        raise ValueError("backup destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)

    # Open the live database read-only to avoid accidental source creation.
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"backup integrity_check failed: {result}")
        finally:
            dst.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("photos.db"))
    parser.add_argument("--out-dir", type=Path, default=Path("backups"))
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = args.out_dir / f"{args.database.stem}-{stamp}.db"
    print(f"Verified SQLite backup: {backup_database(args.database, dest)}")


if __name__ == "__main__":
    main()
