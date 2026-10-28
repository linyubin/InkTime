"""Fail-closed helpers for scanning a photo library over unreliable mounts."""
from pathlib import Path
import os
import sqlite3
import time
from typing import Callable


class ScanIncompleteError(RuntimeError):
    """The scan is incomplete; results must not drive any destructive action."""


def scandir_with_retries(
    directory: Path,
    *,
    attempts: int,
    sleep_seconds: float,
    before_attempt: Callable[[], None] | None = None,
) -> list[os.DirEntry]:
    """Never silently represent an unreadable directory as an empty directory."""
    last_error: OSError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            if before_attempt is not None:
                before_attempt()
            with os.scandir(directory) as entries:
                return list(entries)
        except OSError as exc:
            last_error = exc
            print(f"[WARN] 扫描目录失败 ({attempt}/{max(1, attempts)}): {directory}: {exc}")
            if attempt < max(1, attempts):
                time.sleep(sleep_seconds)
    raise ScanIncompleteError(f"目录扫描不完整，已中止以保护照片数据库: {directory}") from last_error


def count_missing_paths(
    conn: sqlite3.Connection,
    prefixes: list[str],
    escape_like: Callable[[str], str],
) -> int:
    """Report discrepancies against _temp_existing_paths without deleting rows."""
    missing = 0
    for prefix in prefixes:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM photo_scores
            WHERE path LIKE ? ESCAPE '\\'
              AND NOT EXISTS (
                  SELECT 1 FROM _temp_existing_paths t
                  WHERE t.path = photo_scores.path
              )
            """,
            (escape_like(prefix) + "%",),
        ).fetchone()
        missing += int(row[0])
    return missing


def path_is_within_root(path: Path, root: Path) -> bool:
    """Check actual path containment, never a string-prefix match.

    Resolve symlinks so a sibling directory or a symlink escaping the photo
    library cannot inherit NAS remount/retry privileges. Failure fails closed.
    """
    try:
        return Path(path).resolve(strict=False).is_relative_to(
            Path(root).resolve(strict=False)
        )
    except (OSError, ValueError, RuntimeError):
        return False
