"""R1 SQLite Online Backup integration test, including WAL contents."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.backup_sqlite import backup_database


class BackupTests(unittest.TestCase):
    def test_wal_backup_and_source_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "photos.db"
            target = Path(temp) / "backups" / "photos-copy.db"
            with sqlite3.connect(source) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE photo_scores (path TEXT)")
                db.execute("INSERT INTO photo_scores VALUES ('a.jpg')")
                db.commit()
                self.assertEqual(backup_database(source, target), target)
                with sqlite3.connect(target) as copied:
                    self.assertEqual(copied.execute("SELECT path FROM photo_scores").fetchone()[0], "a.jpg")
            with self.assertRaises(FileExistsError):
                backup_database(source, target)


if __name__ == "__main__":
    unittest.main()
