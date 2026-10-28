"""Read-only, two-pass photo reconciliation. Never deletes or modifies photo scores.

A stable scan is evidence for review, NOT authorization to purge records.
Use an isolated staging database and non-production photo mount for acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from db_schema import open_current_readonly_database
from photo_scan_safety import ScanIncompleteError, scandir_with_retries, path_is_within_root

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})


def scan_photo_tree(root: Path, *, attempts: int = 2) -> frozenset[str]:
    """Collect relative image paths; any unreadable directory/metadata fails closed.

    Symlinks are not traversed. This scan intentionally does not apply analyzer
    screenshot/exclusion filters: it checks physical file presence, not selection.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    pending = [root]
    found: set[str] = set()
    while pending:
        directory = pending.pop()
        entries = scandir_with_retries(
            directory, attempts=attempts, sleep_seconds=0,
        )
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    p = Path(entry.path)
                    if p.suffix.lower() in IMAGE_SUFFIXES:
                        found.add(p.relative_to(root).as_posix())
            except OSError as exc:
                raise ScanIncompleteError(
                    f"Unreliable file metadata during reconciliation: {entry.path}"
                ) from exc
    return frozenset(found)


def db_key(db_root: str, relative: str) -> str:
    """Map an observed relative path to an explicitly supplied DB path root."""
    if not db_root or db_root != db_root.strip():
        raise ValueError("db_root must be a non-empty exact database path prefix")
    sep = "\\" if "\\" in db_root and "/" not in db_root else "/"
    base = db_root.rstrip("/\\")
    if not base and sep == "/":
        base = "/"
    if not base:
        raise ValueError("db_root must not be only separators")
    return base + ("" if base == "/" else sep) + relative.replace("/", sep)


def evaluate_missing(
    db_paths: set[str], observed: frozenset[str], db_root: str
) -> tuple[int, list[str]]:
    """Exact-root comparison avoids sibling-prefix matches and double counting."""
    sample = db_key(db_root, "sample.jpg")
    prefix = sample[:-len("sample.jpg")]
    present = {db_key(db_root, p) for p in observed}
    scoped = {p for p in db_paths if p.startswith(prefix)}
    return len(scoped), sorted(scoped - present)


def reconcile(
    database: Path,
    images: Path,
    db_root: str,
    *,
    delay_seconds: float = 30.0,
    minimum_images: int = 1,
    scan=scan_photo_tree,
    sleep=time.sleep,
) -> dict:
    """Two complete matching passes plus read-only v2 DB; no mutations."""
    if not isinstance(minimum_images, int) or minimum_images < 1:
        raise ValueError("minimum_images must be >= 1")
    if not 0 <= delay_seconds <= 86400:
        raise ValueError("delay_seconds must be 0..86400")
    # Validate mapping before doing any I/O.
    db_key(db_root, "probe.jpg")
    root = Path(images).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    first = scan(root)
    if len(first) < minimum_images:
        raise ScanIncompleteError(
            f"First scan has {len(first)} images, below minimum {minimum_images}"
        )
    sleep(delay_seconds)
    second = scan(root)
    if len(second) < minimum_images or first != second:
        raise ScanIncompleteError(
            "Two complete photo scans disagree or fail the minimum count; no report"
        )
    with closing(open_current_readonly_database(Path(database))) as conn:
        db_paths = {str(row[0]) for row in conn.execute("SELECT path FROM photo_scores")}
    scoped, missing = evaluate_missing(db_paths, second, db_root)
    return {
        "status": "review_only_not_authorized_for_deletion",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "image_root": str(root),
        "database_root": db_root,
        "delay_seconds": delay_seconds,
        "minimum_images": minimum_images,
        "first_scan_images": len(first),
        "second_scan_images": len(second),
        "scoped_database_records": scoped,
        "missing_count": len(missing),
        "missing_paths": missing,
    }


def write_report_exclusive(report: Path, data: dict, images: Path, database: Path) -> None:
    """Explicit opt-in output; mode 0600, no overwrite or photo-tree writes."""
    path = Path(report).expanduser().absolute()
    if path_is_within_root(path, Path(images)) or path.resolve() == Path(database).resolve():
        raise ValueError("Report must not be inside photo tree or overwrite database")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--db-root", required=True,
                        help="Exact photo_scores path root; use UNC root for legacy DB paths")
    parser.add_argument("--minimum-images", type=int, default=1,
                        help="Operator-known lower bound for a healthy mounted library")
    parser.add_argument("--delay-seconds", type=float, default=30.0)
    parser.add_argument("--report", type=Path, required=True,
                        help="New, private JSON report path outside photo tree")
    args = parser.parse_args()
    try:
        result = reconcile(
            args.database, args.images, args.db_root,
            delay_seconds=args.delay_seconds, minimum_images=args.minimum_images,
        )
        write_report_exclusive(args.report, result, args.images, args.database)
    except (OSError, sqlite3.Error, ValueError, ScanIncompleteError, RuntimeError) as exc:
        parser.exit(2, f"Reconciliation BLOCKED; no report or deletion: {exc}\n")
    print(
        f"Review-only report: {args.report} | scans={result['first_scan_images']}/"
        f"{result['second_scan_images']} | missing={result['missing_count']}; "
        "no photo_scores rows deleted"
    )


if __name__ == "__main__":
    main()
