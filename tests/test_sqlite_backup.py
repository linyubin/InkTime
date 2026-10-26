"""R1 SQLite online backup and isolated restore tests (including WAL)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup_sqlite import (
    _photo_scores_fingerprint, backup_database, restore_drill,
)


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

    def test_staged_restore_matches_all_rows_and_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "photo #1?.db"
            backup = root / "backup.db"
            restored = root / "staging" / "restore.db"
            with sqlite3.connect(source) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE photo_scores (path TEXT, score REAL, extra BLOB)")
                db.executemany("INSERT INTO photo_scores VALUES (?, ?, ?)", [
                    ("a.jpg", 0.25, b"\x00\xff"),
                    ("b.jpg", None, None),
                    ("a.jpg", 0.25, b"\x00\xff"),
                ])
                db.commit()
                backup_database(source, backup)
                count, digest = restore_drill(backup, restored)
                self.assertEqual(count, 3)
                with sqlite3.connect(restored) as copy:
                    self.assertEqual(_photo_scores_fingerprint(copy), (count, digest))
                    copy.execute("UPDATE photo_scores SET score = 99 WHERE path = 'b.jpg'")
                    self.assertNotEqual(_photo_scores_fingerprint(copy)[1], digest)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 3)

    def test_refuses_existing_restore_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / "backup.db"
            existing = root / "existing.db"
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE photo_scores (path TEXT)")
            existing.write_bytes(b"do not overwrite")
            with self.assertRaises(FileExistsError):
                restore_drill(backup, existing)
            self.assertEqual(existing.read_bytes(), b"do not overwrite")
            with self.assertRaises(ValueError):
                restore_drill(backup, backup)

    def test_corrupt_backup_rejected_without_leaving_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / "bad.db"
            backup.write_bytes(b"not a sqlite database")
            restored = root / "restore.db"
            with self.assertRaises(sqlite3.DatabaseError):
                restore_drill(backup, restored)
            self.assertFalse(restored.exists())

    def test_missing_photo_scores_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / "backup.db"
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE unrelated (x TEXT)")
            restored = root / "restore.db"
            with self.assertRaisesRegex(RuntimeError, "photo_scores table missing"):
                restore_drill(backup, restored)
            self.assertFalse(restored.exists())


if __name__ == "__main__":
    unittest.main()
