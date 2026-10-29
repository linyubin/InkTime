"""R1 staging preflight tests: synthetic, isolated SQLite only."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.init_sqlite import initialize_database
from scripts.staging_preflight import PreflightError, inspect_staging


class StagingPreflightTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.database = self.root / "photos.db"
        self.images = self.root / "photos"
        self.output = self.root / "output"
        self.images.mkdir()
        self.output.mkdir()

    def test_valid_v2_db_does_not_change_folder_contents(self):
        initialize_database(self.database)
        before = {p.name for p in self.root.iterdir()}
        inspect_staging(self.database, self.images, self.output)
        self.assertEqual(before, {p.name for p in self.root.iterdir()})

    def test_missing_db_is_rejected_and_not_created(self):
        with self.assertRaises(PreflightError):
            inspect_staging(self.database, self.images, self.output)
        self.assertFalse(self.database.exists())

    def test_legacy_db_is_rejected_without_migration(self):
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE photo_scores(path TEXT PRIMARY KEY)")
        with self.assertRaises(PreflightError):
            inspect_staging(self.database, self.images, self.output)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (0,))

    def test_non_sqlite_file_is_rejected(self):
        self.database.write_bytes(b"not a database")
        with self.assertRaises(PreflightError):
            inspect_staging(self.database, self.images, self.output)

    def test_missing_image_directory_rejected(self):
        initialize_database(self.database)
        with self.assertRaises(PreflightError):
            inspect_staging(self.database, self.root / "not-there", self.output)

    def test_missing_output_directory_rejected_without_creating(self):
        initialize_database(self.database)
        missing = self.root / "not-there"
        with self.assertRaises(PreflightError):
            inspect_staging(self.database, self.images, missing)
        self.assertFalse(missing.exists())

    def test_cli_success_on_disposable_db(self):
        initialize_database(self.database)
        p = subprocess.run(
            [sys.executable, "-m", "scripts.staging_preflight",
             "--database", str(self.database),
             "--images", str(self.images),
             "--output", str(self.output)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("NOT verified", p.stdout)


if __name__ == "__main__":
    unittest.main()
