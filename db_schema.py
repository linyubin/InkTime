"""Explicit, additive SQLite schema upgrades for InkTime's existing photo database.

This module never migrates on import. The operator CLI takes a verified online
backup while holding SQLite's single-writer lock before any schema change.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 2

# Existing analyze_photos.py columns, including its legacy ALTER TABLE additions.
# Do not reorder, drop, rename or change the meaning of persisted columns.
PHOTO_COLUMNS = {
    "caption": "TEXT",
    "type": "TEXT",
    "memory_score": "REAL",
    "beauty_score": "REAL",
    "reason": "TEXT",
    "width": "INTEGER",
    "height": "INTEGER",
    "orientation": "TEXT",
    "used_at": "TEXT",
    "exif_json": "TEXT",
    "raw_json": "TEXT",
    "exif_datetime": "TEXT",
    "exif_make": "TEXT",
    "exif_model": "TEXT",
    "exif_iso": "INTEGER",
    "exif_exposure_time": "REAL",
    "exif_f_number": "REAL",
    "exif_focal_length": "REAL",
    "exif_gps_lat": "REAL",
    "exif_gps_lon": "REAL",
    "exif_gps_alt": "REAL",
    "side_caption": "TEXT",
    "exif_city": "TEXT",
    "subjects_json": "TEXT",
}
TRANSFER_COLUMNS = {
    "id", "ts", "method", "path", "idx", "status", "bytes",
    "duration_ms", "client_ip", "user_agent",
}
INDEXES = {"idx_photo_scores_exif_datetime", "idx_photo_scores_md"}


class MigrationError(RuntimeError):
    """Unsupported or inconsistent database schema; do not continue writing."""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Only constant, known table names reach this function.
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _require_photo_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'photo_scores'"
    ).fetchone()
    if row != ("table",) or "path" not in _columns(conn, "photo_scores"):
        raise MigrationError("expected existing photo_scores table with path column")


def _verify_photo_columns(conn: sqlite3.Connection) -> None:
    missing = set(PHOTO_COLUMNS).difference(_columns(conn, "photo_scores"))
    if missing:
        raise MigrationError(f"photo_scores missing required columns: {sorted(missing)}")


def verify_latest_schema(conn: sqlite3.Connection) -> None:
    _require_photo_table(conn)
    _verify_photo_columns(conn)
    if not TRANSFER_COLUMNS.issubset(_columns(conn, "transfer_history")):
        raise MigrationError("transfer_history missing required columns")
    existing_indexes = {
        row[1] for row in conn.execute('PRAGMA index_list("photo_scores")')
    }
    if not INDEXES.issubset(existing_indexes):
        raise MigrationError("photo_scores missing required indexes")


def current_version(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise MigrationError(
            f"database user_version {version} is newer than supported {SCHEMA_VERSION}"
        )
    return version


def require_current_schema(database: Path) -> None:
    """Read-only startup gate: require an existing, fully migrated v2 database.

    Never creates a missing DB, changes schema, or implicitly upgrades legacy
    data. Operators must migrate an offline staging copy with a verified backup.
    """
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"existing SQLite database required: {path}")
    try:
        # Path.as_uri() percent-encodes '#' and '?' in filenames.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            version = current_version(conn)
            if version != SCHEMA_VERSION:
                raise MigrationError(
                    f"database schema v{version}; expected v{SCHEMA_VERSION}. "
                    "Run explicit backed-up migration before starting server."
                )
            verify_latest_schema(conn)
    except sqlite3.Error as exc:
        raise MigrationError(f"database preflight failed: {exc}") from exc



def open_current_database(
    database: Path, *, busy_timeout_ms: int = 5000
) -> sqlite3.Connection:
    """Open an existing v2 database for analyzer writes; never auto-migrate.

    Validate again on the actual writable connection to avoid relying solely
    on an earlier read-only preflight. The caller owns and must close the
    returned connection. Failed validation always closes it.
    """
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool) or not 1 <= busy_timeout_ms <= 60000:
        raise ValueError("busy_timeout_ms must be 1..60000")
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"existing SQLite database required: {path}")
    try:
        conn = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True,
            timeout=busy_timeout_ms / 1000,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            version = current_version(conn)
            if version != SCHEMA_VERSION:
                raise MigrationError(
                    f"database schema v{version}; expected v{SCHEMA_VERSION}. "
                    "Run explicit backed-up migration before starting analyzer."
                )
            verify_latest_schema(conn)
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise MigrationError(f"SQLite WAL unavailable: {mode}")
            return conn
        except BaseException:
            conn.close()
            raise
    except sqlite3.Error as exc:
        raise MigrationError(f"analyzer database connection failed: {exc}") from exc


def apply_migrations(conn: sqlite3.Connection) -> tuple[int, int]:
    """Upgrade an existing DB inside the caller's explicit transaction.

    Caller must BEGIN IMMEDIATE, verify a backup, then COMMIT or ROLLBACK.
    SQLite DDL and user_version updates are transactional.
    """
    if not conn.in_transaction:
        raise MigrationError("migration requires an explicit transaction")
    before = current_version(conn)
    _require_photo_table(conn)
    if before >= 1:
        _verify_photo_columns(conn)

    if before < 1:
        existing = _columns(conn, "photo_scores")
        for name, column_type in PHOTO_COLUMNS.items():
            if name not in existing:
                conn.execute(
                    f'ALTER TABLE "photo_scores" ADD COLUMN "{name}" {column_type}'
                )
        conn.execute("PRAGMA user_version = 1")

    if before < 2:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transfer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                method TEXT,
                path TEXT,
                idx INTEGER,
                status INTEGER,
                bytes INTEGER,
                duration_ms REAL,
                client_ip TEXT,
                user_agent TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photo_scores_exif_datetime "
            "ON photo_scores(exif_datetime)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photo_scores_md "
            "ON photo_scores(substr(exif_datetime, 6, 2) || '-' || "
            "substr(exif_datetime, 9, 2))"
        )
        conn.execute("PRAGMA user_version = 2")

    verify_latest_schema(conn)
    return before, current_version(conn)
