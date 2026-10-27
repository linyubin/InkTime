"""R1 PR-03: isolated, offline, versioned SQLite migration safety tests."""
from __future__ import annotations

import ast
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from db_schema import SCHEMA_VERSION, MigrationError, PHOTO_COLUMNS, apply_migrations, require_current_schema, open_current_database, open_current_readonly_database
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
                ("one.jpg", bytes([0, 255])),
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
            self.assertEqual(conn.execute("SELECT raw_json FROM photo_scores WHERE path='one.jpg'").fetchone()[0], bytes([0, 255]))
            self.assertTrue(set(PHOTO_COLUMNS).issubset({
                row[1] for row in conn.execute('PRAGMA table_info("photo_scores")')
            }))
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transfer_history'"
            ).fetchone())
        with sqlite3.connect(result.backup_path) as original:
            self.assertEqual(_photo_scores_fingerprint(original), before)

    def test_runtime_gate_accepts_migrated_v2_without_schema_mutation(self):
        migrate_database(self.db, self.backups)
        with sqlite3.connect(self.db) as conn:
            before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
            row_count = conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()
        require_current_schema(self.db)
        require_current_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(before, conn.execute(
                "SELECT sql FROM sqlite_master ORDER BY name").fetchall())
            self.assertEqual(row_count, conn.execute(
                "SELECT COUNT(*) FROM photo_scores").fetchone())

    def test_runtime_gate_rejects_legacy_without_upgrading(self):
        with sqlite3.connect(self.db) as conn:
            before = _photo_scores_fingerprint(conn)
        with self.assertRaisesRegex(MigrationError, "expected v2"):
            require_current_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(_photo_scores_fingerprint(conn), before)
        self.assertFalse(self.backups.exists())

    def test_runtime_gate_rejects_missing_db_without_creating_it(self):
        absent = self.root / "missing.db"
        with self.assertRaisesRegex(MigrationError, "existing SQLite database"):
            require_current_schema(absent)
        self.assertFalse(absent.exists())

    def test_runtime_gate_rejects_future_version(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(MigrationError):
            require_current_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_runtime_gate_rejects_corrupt_v2_schema(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=2")
        with self.assertRaises(MigrationError):
            require_current_schema(self.db)

    def test_runtime_gate_supports_uri_reserved_filename(self):
        migrate_database(self.db, self.backups)
        require_current_schema(self.db)  # fixture filename contains '#' and '?'

    def test_runtime_gate_rejects_non_database_file(self):
        path = self.root / "not_sqlite.db"
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(MigrationError):
            require_current_schema(path)
        self.assertEqual(path.read_bytes(), b"not a sqlite database")

    def test_server_preflight_precedes_startup_writes(self):
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        tree = ast.parse(source)
        # No implicit CREATE TABLE / CREATE INDEX or ignored schema errors.
        self.assertNotIn("CREATE TABLE IF NOT EXISTS transfer_history", source)
        self.assertNotIn("CREATE INDEX IF NOT EXISTS idx_photo_scores", source)
        self.assertNotIn("def ensure_transfer_history(", source)
        self.assertLess(source.index("require_current_schema(DB_PATH)"),
                        source.index("BIN_OUTPUT_DIR.mkdir("))
        self.assertLess(source.index("require_current_schema(DB_PATH)"),
                        source.index("_init_db_pragmas()\n\n#"))
        self.assertIn("raise SystemExit(f\"InkTime database startup blocked:", source)
        self.assertIsInstance(tree, ast.Module)

    def test_repeat_is_idempotent_and_creates_no_second_backup(self):
        first = migrate_database(self.db, self.backups)
        second = migrate_database(self.db, self.backups)
        self.assertIsNone(second.backup_path)
        self.assertEqual((second.previous_version, second.version), (2, 2))
        self.assertEqual(list(self.backups.glob("*.db")), [first.backup_path])

    def test_v1_to_v2_adds_only_transfer_history_and_indexes(self):
        migrate_database(self.db, self.backups)
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE transfer_history")
            conn.execute("DROP INDEX idx_photo_scores_exif_datetime")
            conn.execute("DROP INDEX idx_photo_scores_md")
            conn.execute("PRAGMA user_version=1")
            columns_before = {
                row[1] for row in conn.execute('PRAGMA table_info("photo_scores")')
            }
        second = migrate_database(self.db, self.backups)
        self.assertEqual((second.previous_version, second.version), (1, 2))
        with sqlite3.connect(self.db) as conn:
            columns_after = {
                row[1] for row in conn.execute('PRAGMA table_info("photo_scores")')
            }
            self.assertEqual(columns_before, columns_after)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

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

    def test_analyzer_connection_requires_existing_migrated_v2_and_wal(self):
        migrate_database(self.db, self.backups)
        with closing(open_current_database(self.db, busy_timeout_ms=1250)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1250)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 2)
            conn.execute("UPDATE photo_scores SET caption=? WHERE path=?", ("ok", "one.jpg"))
            conn.commit()
        with sqlite3.connect(self.db) as check:
            self.assertEqual(check.execute(
                "SELECT caption FROM photo_scores WHERE path='one.jpg'"
            ).fetchone()[0], "ok")

    def test_analyzer_connection_refuses_legacy_without_schema_writes(self):
        with sqlite3.connect(self.db) as conn:
            original = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        with self.assertRaisesRegex(MigrationError, "expected v2"):
            open_current_database(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), original)

    def test_analyzer_connection_refuses_missing_database_without_creation(self):
        absent = self.root / "no-db.db"
        with self.assertRaisesRegex(MigrationError, "existing SQLite database"):
            open_current_database(absent)
        self.assertFalse(absent.exists())

    def test_analyzer_connection_refuses_incomplete_v2(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=2")
        with self.assertRaises(MigrationError):
            open_current_database(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='transfer_history'"
            ).fetchone())

    def test_analyzer_connection_refuses_future_schema(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(MigrationError):
            open_current_database(self.db)

    def test_analyzer_connection_rejects_invalid_busy_timeout(self):
        migrate_database(self.db, self.backups)
        for invalid in (0, -1, 60001, True, 1.5, "5000"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                open_current_database(self.db, busy_timeout_ms=invalid)

    def test_webui_readonly_connection_accepts_v2_and_cannot_write(self):
        migrate_database(self.db, self.backups)
        with closing(open_current_readonly_database(self.db, busy_timeout_ms=1250)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1250)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE photo_scores SET caption='bad'")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE bad_schema_change (id INTEGER)")

    def test_webui_readonly_connection_rejects_legacy_without_modification(self):
        with sqlite3.connect(self.db) as conn:
            before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        with self.assertRaisesRegex(MigrationError, "expected v2"):
            open_current_readonly_database(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(before, conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())

    def test_webui_readonly_connection_rejects_missing_file(self):
        missing = self.root / "missing_webui.db"
        with self.assertRaisesRegex(MigrationError, "existing SQLite database"):
            open_current_readonly_database(missing)
        self.assertFalse(missing.exists())

    def test_webui_readonly_connection_rejects_corrupt_v2(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=2")
        with self.assertRaises(MigrationError):
            open_current_readonly_database(self.db)

    def test_webui_readonly_connection_rejects_future_version(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(MigrationError):
            open_current_readonly_database(self.db)

    def test_webui_readonly_connection_rejects_invalid_timeouts(self):
        for invalid in (0, -1, 60001, True, 1.5, "5000"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                open_current_readonly_database(self.db, busy_timeout_ms=invalid)

    def test_webui_readonly_connection_source_never_falls_back_to_write(self):
        source = (Path(__file__).resolve().parents[1] / "webui.py").read_text()
        tree = ast.parse(source)
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_get_conn")
        body = ast.get_source_segment(source, helper)
        self.assertIn("open_current_readonly_database(DB_PATH)", body)
        self.assertNotIn("sqlite3.connect(", source)
        self.assertNotIn("return None", body)

    def test_analyzer_source_no_implicit_ddl_and_preflights_before_filelist(self):
        source = (Path(__file__).resolve().parents[1] / "analyze_photos.py").read_text()
        tree = ast.parse(source)
        self.assertNotIn("def ensure_table(", source)
        self.assertNotIn("ALTER TABLE photo_scores", source)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS photo_scores", source)
        self.assertNotIn("sqlite3.connect(DB_PATH)", source)
        self.assertIn("conn = open_current_database(DB_PATH)", source)
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        lines = source.splitlines()
        main_source = "\n".join(lines[main.lineno - 1:main.end_lineno])
        self.assertLess(main_source.index("require_current_schema(DB_PATH)"),
                        main_source.index("list_images()"))
        self.assertLess(main_source.index("require_current_schema(DB_PATH)"),
                        main_source.index('filelist_path.write_text('))
        self.assertLess(main_source.index("require_current_schema(DB_PATH)"),
                        main_source.index("open_current_database(DB_PATH)"))


if __name__ == "__main__":
    unittest.main()
