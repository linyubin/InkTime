"""R1 PR-01: analyzer-level scan failure injection, no real NAS or model calls."""
from importlib.util import find_spec
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
from scripts.init_sqlite import initialize_database
from photo_scan_safety import ScanIncompleteError

mode = sys.argv[1]
with tempfile.TemporaryDirectory(prefix="inktime-scan-smoke-") as d:
    root = Path(d)
    photos = root / "photos"
    child = photos / "nested"
    child.mkdir(parents=True)
    (photos / "a.jpg").write_bytes(b"synthetic fixture")
    (child / "b.png").write_bytes(b"synthetic fixture")
    db = root / "photos.db"
    initialize_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO photo_scores (path) VALUES (?)", ("previously-scored.jpg",))
        before = conn.execute("SELECT path FROM photo_scores").fetchall()
    snapshot = root / "filelist.txt"
    snapshot.write_text("KEEP THIS SNAPSHOT", encoding="utf-8")

    cfg = types.ModuleType("config")
    cfg.IMAGE_DIR = str(photos)
    cfg.DB_PATH = str(db)
    cfg.NAS_RETRY_TIMES = 2
    cfg.NAS_RETRY_SLEEP_SEC = 0
    cfg.EXCLUDE_KEYWORDS = []
    if mode == "remount_log":
        cfg.NAS_MOUNT_URL = "smb://user:very-secret@nas.local/private"
    sys.modules["config"] = cfg
    import analyze_photos as analyzer
    analyzer.ROOT_DIR = root
    real_scandir = os.scandir
    calls = {"child": 0}

    class BadEntry:
        path = str(photos / "broken.jpg")
        def is_dir(self, follow_symlinks=False):
            if mode == "stat_dir":
                raise OSError("NAS metadata unavailable")
            return False
        def is_file(self, follow_symlinks=False):
            raise OSError("NAS metadata unavailable")

    def fake_scandir(path):
        location = Path(path)
        if mode == "root" and location == photos:
            raise OSError("NAS offline")
        if mode in ("child", "transient") and location == child:
            calls["child"] += 1
            if mode == "child" or calls["child"] == 1:
                raise OSError("NAS child offline")
        if mode in ("stat_dir", "stat_file") and location == photos:
            class ScanResult:
                def __enter__(self):
                    return iter([BadEntry()])
                def __exit__(self, *args):
                    pass
            return ScanResult()
        return real_scandir(path)

    with patch("photo_scan_safety.os.scandir", side_effect=fake_scandir):
        if mode == "remount_log":
            import io
            from contextlib import redirect_stdout
            with patch.object(analyzer.subprocess, "run") as run, \
                    patch.object(analyzer, "_is_mount_ok", return_value=True), \
                    redirect_stdout(io.StringIO()) as out:
                assert analyzer._try_remount_nas()
            assert run.called
            assert "very-secret" not in out.getvalue(), out.getvalue()
            assert "user:" not in out.getvalue(), out.getvalue()
        elif mode == "transient":
            found = analyzer.list_images()
            assert {p.name for p in found} == {"a.jpg", "b.png"}, found
            assert calls["child"] == 2, calls
        else:
            try:
                analyzer.main()
            except ScanIncompleteError:
                pass
            else:
                raise AssertionError("Incomplete scan was accepted")
    assert snapshot.read_text(encoding="utf-8") == "KEEP THIS SNAPSHOT"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT path FROM photo_scores").fetchall() == before
'''

@unittest.skipUnless(find_spec("requests") and find_spec("PIL"),
                     "Analyzer import requires installed runtime dependencies")
class AnalyzerScanSmokeTests(unittest.TestCase):
    def _run(self, mode):
        p = subprocess.run([sys.executable, "-c", CHILD, mode],
                           cwd=ROOT, capture_output=True, text=True, timeout=45)
        self.assertEqual(p.returncode, 0,
                         f"mode={mode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")

    def test_unreadable_root_preserves_snapshot_and_scores(self):
        self._run("root")

    def test_unreadable_child_preserves_snapshot_and_scores(self):
        self._run("child")

    def test_direntry_is_dir_error_preserves_snapshot_and_scores(self):
        self._run("stat_dir")

    def test_direntry_is_file_error_preserves_snapshot_and_scores(self):
        self._run("stat_file")

    def test_transient_child_error_retries_to_complete_snapshot(self):
        self._run("transient")

    def test_nas_remount_logging_hides_url_credentials(self):
        self._run("remount_log")


if __name__ == "__main__":
    unittest.main()
