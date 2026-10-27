"""R1 PR-03: isolated, offline, versioned SQLite migration safety tests."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from db_schema import SCHEMA_VERSION, MigrationError, PHOTO_COLUMNS, apply_migrations
from scripts.backup_sqlite import _photo_scores_fingerprint, restore_drill
from scripts.migrate_sqlite import inspect_database, migrate_database


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "photo #1?.db"
        self.backups = self.root / "backups"
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE photo_scores(path TEXT PRIMARY KEY, raw_json BLOB)")
            conn.executemany("INSERT INTO photo_scores VALUES (?, ?)", [
                ("one.jpg", b"\\x00\\xff"),
                ("two.jpg", None),
            ])

    def test_legacy_migration_is_additive_preserves_all_rows_and_wal(self):
        with sqlite3.connect(self.db) as conn:
            before = _photo_scores_fingerprint(conn)
        self.assertEqual(inspect_database(self.db), 0)
        result = migrate_database(self.db, self.backups)
        self.assertEqual((result.previous_version, result.version), (0, SCHEMA_VERSION))
        self.assertTrue(result.backup_path.is_file())
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT raw_json FROM photo_scores WHERE path='one.jpg'").fetchone()[0], b"\\x00\\xff")
            self.assertTrue(set(PHOTO_COLUMNS).issubset({
                row[1] for row in conn.execute('PRAGMA table_info("photo_scores")')
            }))
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transfer_history'"
            ).fetchone())
        with sqlite3.connect(result.backup_path) as original:
            self.assertEqual(_photo_scores_fingerprint(original), before)

    def test_repeat_is_idempotent_and_creates_no_second_backup(self):
        first = migrate_database(self.db, self.backups)
        second = migrate_database(self.db, self.backups)
        self.assertIsNone(second.backup_path)
        self.assertEqual((second.previous_version, second.version), (2, 2))
        self.assertEqual(list(self.backups.glob("*.db")), [first.backup_path])

    def test_failure_rolls_back_schema_and_version_and_retains_backup(self):
        with sqlite3.connect(self.db) as conn:
            # Collides with migration v2 index name after v1 columns are added.
            conn.execute("CREATE TABLE idx_photo_scores_exif_datetime (x TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            migrate_database(self.db, self.backups)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual({r[1] for r in conn.execute('PRAGMA table_info("photo_scores")')},
                             {"path", "raw_json"})
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transfer_history'"
            ).fetchone())
        backups = list(self.backups.glob("*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        with tempfile.TemporaryDirectory() as dest:
            restore_drill(backups[0], Path(dest) / "restored.db")

    def test_future_version_fails_closed_without_backup(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(MigrationError):
            migrate_database(self.db, self.backups)
        self.assertFalse(self.backups.exists())
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_missing_photo_scores_rejected_without_backup(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE photo_scores")
        with self.assertRaises(MigrationError):
            migrate_database(self.db, self.backups)
        self.assertFalse(self.backups.exists())

    def test_unsupported_existing_schema_v2_is_not_silently_accepted(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=2")
        with self.assertRaises(MigrationError):
            inspect_database(self.db)
        with self.assertRaises(MigrationError):
            migrate_database(self.db, self.backups)
        self.assertFalse(self.backups.exists())

    def test_migration_requires_transaction(self):
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(MigrationError):
                apply_migrations(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_busy_writer_refuses_without_creating_backup(self):
        writer = sqlite3.connect(self.db, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                migrate_database(self.db, self.backups, busy_timeout_ms=100)
            self.assertFalse(self.backups.exists())
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_cli_is_read_only_by_default_and_apply_requires_backup_dir(self):
        cwd = Path(__file__).resolve().parents[1]
        command = [sys.executable, "-m", "scripts.migrate_sqlite", "--database", str(self.db)]
        check = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Read-only schema check: v0", check.stdout)
        refused = subprocess.run(command + ["--apply"], cwd=cwd,
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--backup-dir", refused.stderr)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    unittest.main()
