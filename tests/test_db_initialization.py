"""PR-03: isolated tests for explicit, no-clobber first-install SQLite initialization."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
from unittest import mock
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from db_schema import SCHEMA_VERSION, open_current_database, open_current_readonly_database, require_current_schema
from scripts.init_sqlite import initialize_database


class FirstInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "photos #1?.db"

    def test_explicit_init_produces_empty_current_schema(self):
        self.assertEqual(initialize_database(self.db), self.db)
        require_current_schema(self.db)
        with closing(open_current_readonly_database(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (SCHEMA_VERSION,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transfer_history").fetchone(), (0,))
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        with closing(open_current_database(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            conn.execute("INSERT INTO photo_scores(path) VALUES (?)", ("first.jpg",))
            conn.commit()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT path FROM photo_scores").fetchone(), ("first.jpg",))

    def test_existing_database_never_overwritten(self):
        self.db.write_bytes(b"existing bytes, not SQLite")
        with self.assertRaises(FileExistsError):
            initialize_database(self.db)
        self.assertEqual(self.db.read_bytes(), b"existing bytes, not SQLite")

    def test_repeated_initialization_does_not_erase_rows(self):
        initialize_database(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO photo_scores(path) VALUES ('keep.jpg')")
        with self.assertRaises(FileExistsError):
            initialize_database(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT path FROM photo_scores").fetchone(), ("keep.jpg",))

    def test_existing_dangling_symlink_is_not_followed(self):
        missing = self.root / "other.db"
        try:
            self.db.symlink_to(missing)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(FileExistsError):
            initialize_database(self.db)
        self.assertFalse(missing.exists())
        self.assertTrue(self.db.is_symlink())

    def test_missing_parent_does_not_create_directories(self):
        missing = self.root / "not-created" / "photos.db"
        with self.assertRaises(FileNotFoundError):
            initialize_database(missing)
        self.assertFalse(missing.parent.exists())

    def test_publish_failure_cleans_temp_and_leaves_target_absent(self):
        with mock.patch("scripts.init_sqlite.os.link", side_effect=OSError("link unsupported")):
            with self.assertRaisesRegex(OSError, "link unsupported"):
                initialize_database(self.db)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.root.glob(".photos*.init-*")), [])

    def test_schema_creation_failure_cleans_temp(self):
        with mock.patch("scripts.init_sqlite.apply_migrations", side_effect=RuntimeError("DDL failure")):
            with self.assertRaisesRegex(RuntimeError, "DDL failure"):
                initialize_database(self.db)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.root.glob(".photos*.init-*")), [])

    def test_concurrent_initializers_only_one_publishes(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self._attempt_initialize, range(2)))
        self.assertEqual(sorted(results), ["created", "exists"])
        require_current_schema(self.db)

    def _attempt_initialize(self, _):
        try:
            initialize_database(self.db)
            return "created"
        except FileExistsError:
            return "exists"

    @unittest.skipUnless(os.name == "posix", "POSIX mode only")
    def test_created_file_is_private(self):
        initialize_database(self.db)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    def test_cli_defaults_to_dry_run_and_requires_explicit_create(self):
        cwd = Path(__file__).resolve().parents[1]
        cmd = [sys.executable, "-m", "scripts.init_sqlite", "--database", str(self.db)]
        dry = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Dry run only", dry.stdout)
        self.assertFalse(self.db.exists())
        created = subprocess.run(cmd + ["--create"], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        require_current_schema(self.db)
        again = subprocess.run(cmd + ["--create"], cwd=cwd, capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("FileExistsError", again.stderr)


if __name__ == "__main__":
    unittest.main()
