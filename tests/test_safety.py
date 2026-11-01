"""R1 regression tests: no destructive fallback after scan failures."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from path_security import safe_join
from photo_scan_safety import ScanIncompleteError, scandir_with_retries, count_missing_paths, path_is_within_root


class SafePathTests(unittest.TestCase):
    def test_valid_nested_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(safe_join(root, "dir/image.jpg"), root / "dir" / "image.jpg")

    def test_blocks_parent_traversal_and_prefix_sibling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "photos"
            root.mkdir()
            with self.assertRaises(ValueError):
                safe_join(root, "../photos-private/secret.jpg")
            with self.assertRaises(ValueError):
                safe_join(root, "../../etc/passwd")

    def test_blocks_absolute_paths_and_outside_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "photos"
            root.mkdir()
            other = Path(temp) / "private"
            other.mkdir()
            (root / "escape").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                safe_join(root, str(other / "secret.jpg"))
            with self.assertRaises(ValueError):
                safe_join(root, "escape/secret.jpg")

    def test_photo_root_containment_rejects_prefix_siblings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Photos"
            sibling = Path(temp) / "Photos-private"
            root.mkdir()
            sibling.mkdir()
            self.assertTrue(path_is_within_root(root / "valid.jpg", root))
            self.assertFalse(path_is_within_root(sibling / "secret.jpg", root))

    def test_photo_root_containment_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Photos"
            outside = Path(temp) / "private"
            root.mkdir()
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            self.assertFalse(path_is_within_root(root / "link" / "secret.jpg", root))


class ScanSafetyTests(unittest.TestCase):
    def test_unreadable_root_is_not_empty_scan(self):
        with patch("photo_scan_safety.os.scandir", side_effect=OSError("NAS offline")) as scandir:
            with self.assertRaises(ScanIncompleteError):
                scandir_with_retries(Path("/missing"), attempts=3, sleep_seconds=0)
            self.assertEqual(scandir.call_count, 3)

    def test_existing_empty_directory_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(scandir_with_retries(Path(temp), attempts=1, sleep_seconds=0), [])

    def test_missing_rows_are_reported_not_deleted(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE photo_scores (path TEXT)")
            conn.execute("CREATE TEMP TABLE _temp_existing_paths (path TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO photo_scores VALUES (?)", [
                ("/photos/a.jpg",), ("/photos/b.jpg",)
            ])
            conn.execute("INSERT INTO _temp_existing_paths VALUES (?)", ("/photos/a.jpg",))
            self.assertEqual(count_missing_paths(conn, ["/photos"], lambda x: x), 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
