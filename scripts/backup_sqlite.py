#!/usr/bin/env python3
"""Create a consistent SQLite backup and optionally rehearse an isolated restore."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


def _readonly_connection(path: Path) -> sqlite3.Connection:
    # Path.as_uri() percent-escapes spaces, '#' and '?' in file names.
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _integrity_check(db: sqlite3.Connection, label: str) -> None:
    result = db.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"{label} integrity_check failed: {result}")


def _photo_scores_fingerprint(db: sqlite3.Connection) -> tuple[int, str]:
    """Stable, streaming digest of every photo_scores column and row.

    SQL quote() normalizes NULL, numeric values, text and BLOBs. Sorting
    their quoted representations makes the digest independent of rowid and
    insertion order, while preserving duplicate rows.
    """
    schema = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photo_scores'"
    ).fetchone()
    if schema is None:
        raise RuntimeError("photo_scores table missing; restore drill cannot compare records")
    columns = db.execute('PRAGMA table_info("photo_scores")').fetchall()
    if not columns:
        raise RuntimeError("photo_scores table has no columns")
    expressions = [f'quote("{column[1].replace(chr(34), chr(34) * 2)}")' for column in columns]
    projection = ", ".join(expressions)
    digest = hashlib.sha256()
    digest.update(schema[0].encode("utf-8"))
    digest.update(b"\0")
    count = 0
    for row in db.execute(f'SELECT {projection} FROM "photo_scores" ORDER BY {projection}'):
        count += 1
        for value in row:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return count, digest.hexdigest()


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

    src = _readonly_connection(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
            _integrity_check(dst, "backup")
        finally:
            dst.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    return destination


def restore_drill(backup: Path, restored: Path) -> tuple[int, str]:
    """Restore a backup to a *new* database and compare all photo score rows.

    This function never writes to the original database or an existing target.
    The caller chooses a disposable staging location for the restored copy.
    """
    backup = backup.expanduser().resolve()
    restored = restored.expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"SQLite backup not found: {backup}")
    if backup == restored:
        raise ValueError("restore target must differ from backup")
    if restored.exists():
        raise FileExistsError(restored)
    restored.parent.mkdir(parents=True, exist_ok=True)

    src = _readonly_connection(backup)
    try:
        _integrity_check(src, "backup source")
        before = _photo_scores_fingerprint(src)
        dst = sqlite3.connect(restored)
        try:
            src.backup(dst)
            dst.commit()
            _integrity_check(dst, "restored copy")
            after = _photo_scores_fingerprint(dst)
            if after != before:
                raise RuntimeError("restored photo_scores rows or schema differ from backup")
        finally:
            dst.close()
    except Exception:
        restored.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    return before


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("photos.db"))
    parser.add_argument("--out-dir", type=Path, default=Path("backups"))
    parser.add_argument("--restore-drill", action="store_true",
                        help="restore to a temporary DB and compare all photo_scores records")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = args.out_dir / f"{args.database.stem}-{stamp}.db"
    print(f"Verified SQLite backup: {backup_database(args.database, dest)}")
    if args.restore_drill:
        with TemporaryDirectory(prefix="inktime-restore-drill-") as temp:
            count, digest = restore_drill(dest, Path(temp) / "restored.db")
            print(f"Isolated restore verified: photo_scores={count}, sha256={digest}")


if __name__ == "__main__":
    main()
