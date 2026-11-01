"""R1 two-scan reconciliation: disposable files and SQLite only."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.init_sqlite import initialize_database
from scripts.reconcile_photos import (
    db_key, evaluate_missing, reconcile, scan_photo_tree, write_report_exclusive,
)
from photo_scan_safety import ScanIncompleteError


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inktime-reconcile-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "photos"
        self.images.mkdir()
        (self.images / "a.jpg").write_bytes(b"a")
        (self.images / "b.png").write_bytes(b"b")
        self.db = self.root / "photos.db"
        initialize_database(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.executemany("INSERT INTO photo_scores (path) VALUES (?)", [
                (str(self.images / "a.jpg"),),
                (str(self.images / "deleted.jpg"),),
                (str(self.root / "photos-private" / "not-in-scope.jpg"),),
            ])

    def test_two_stable_scans_report_missing_without_db_write(self):
        result = reconcile(self.db, self.images, str(self.images),
                           delay_seconds=0, minimum_images=2)
        self.assertEqual(result["status"], "review_only_not_authorized_for_deletion")
        self.assertEqual(result["missing_paths"], [str(self.images / "deleted.jpg")])
        self.assertEqual(result["scoped_database_records"], 2)
        self.assertEqual((result["first_scan_images"], result["second_scan_images"]), (2, 2))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 3)

    def test_scans_disagree_fails_without_report(self):
        calls = iter([frozenset({"a.jpg", "b.png"}), frozenset({"a.jpg"})])
        with self.assertRaises(ScanIncompleteError):
            reconcile(self.db, self.images, str(self.images), delay_seconds=0,
                      scan=lambda _: next(calls))

    def test_scan_failure_fails_closed(self):
        calls = [0]
        def scan(_):
            calls[0] += 1
            if calls[0] == 2:
                raise ScanIncompleteError("NAS disconnected")
            return frozenset({"a.jpg", "b.png"})
        with self.assertRaises(ScanIncompleteError):
            reconcile(self.db, self.images, str(self.images), delay_seconds=0, scan=scan)
        self.assertEqual(calls[0], 2)

    def test_empty_or_too_small_scan_is_rejected(self):
        with self.assertRaises(ScanIncompleteError):
            reconcile(self.db, self.images, str(self.images), delay_seconds=0,
                      minimum_images=3)

    def test_exact_prefix_and_windows_unc_mapping(self):
        count, missing = evaluate_missing(
            {r"\\nas\Photos\a.jpg", r"\\nas\Photos\gone.jpg",
             r"\\nas\Photos-private\unrelated.jpg"},
            frozenset({"a.jpg"}), r"\\nas\Photos")
        self.assertEqual((count, missing), (2, [r"\\nas\Photos\gone.jpg"]))
        self.assertEqual(db_key("/mnt/Photos", "sub/a.jpg"), "/mnt/Photos/sub/a.jpg")

    def test_report_private_exclusive_and_outside_library(self):
        data = reconcile(self.db, self.images, str(self.images), delay_seconds=0)
        report = self.root / "review.json"
        write_report_exclusive(report, data, self.images, self.db)
        self.assertEqual(json.loads(report.read_text())["missing_count"], 1)
        self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_report_exclusive(report, data, self.images, self.db)
        with self.assertRaises(ValueError):
            write_report_exclusive(self.images / "report.json", data, self.images, self.db)

    def test_no_symlink_traversal_and_nested_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"private")
        (self.images / "escape").symlink_to(outside, target_is_directory=True)
        sub = self.images / "sub"
        sub.mkdir()
        (sub / "nested.webp").write_bytes(b"nested")
        self.assertEqual(scan_photo_tree(self.images),
                         frozenset({"a.jpg", "b.png", "sub/nested.webp"}))

    def test_invalid_root_or_delay_fails(self):
        with self.assertRaises(ValueError):
            reconcile(self.db, self.images, "", delay_seconds=0)
        with self.assertRaises(ValueError):
            reconcile(self.db, self.images, str(self.images), delay_seconds=-1)
        with self.assertRaises(ValueError):
            reconcile(self.db, self.images, str(self.images), minimum_images=0)


if __name__ == "__main__":
    unittest.main()
