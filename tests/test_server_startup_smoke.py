"""R1 PR-03: Flask startup/HTTP boundary smoke tests with disposable SQLite only.

The server is imported in a child interpreter so a fake config never escapes
into the other unit tests. No production DB, NAS mount or real device is used.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from importlib.util import find_spec

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import base64
import os
import sys
import tempfile
import types
from pathlib import Path

from scripts.init_sqlite import initialize_database

mode = sys.argv[1]
with tempfile.TemporaryDirectory(prefix="inktime-flask-smoke-") as d:
    root = Path(d)
    db_path = root / "photos.db"
    photos = root / "photos"
    output = root / "output"
    photos.mkdir()
    config = types.ModuleType("config")
    config.DB_PATH = str(db_path)
    config.IMAGE_DIR = str(photos)
    config.BIN_OUTPUT_DIR = str(output)
    config.FLASK_HOST = "127.0.0.1"
    config.FLASK_PORT = 8765 if mode != "invalid" else "bad-port"
    config.ENABLE_REVIEW_WEBUI = False
    config.DAILY_PHOTO_QUANTITY = 5
    config.DOWNLOAD_KEY = "smoke-device-key"
    sys.modules["config"] = config

    for key in ("INKTIME_DOWNLOAD_KEY", "INKTIME_DOWNLOAD_KEY_PREVIOUS",
                "INKTIME_ADMIN_USERNAME", "INKTIME_ADMIN_PASSWORD"):
        os.environ.pop(key, None)

    if mode == "valid":
        initialize_database(db_path)

    try:
        import server
    except SystemExit as exc:
        assert mode in ("missing", "invalid"), repr(exc)
        message = str(exc)
        if mode == "missing":
            assert "database startup blocked" in message, message
        else:
            assert "Invalid server configuration" in message, message
        assert not db_path.exists(), "Invalid config/missing DB created a database"
        assert not output.exists(), "Invalid startup wrote to output directory"
    else:
        assert mode == "valid", "Unsafe startup unexpectedly succeeded"
        assert server.DB_PATH == db_path
        assert db_path.exists()
        app = server.app
        app.testing = True
        with app.test_client() as client:
            # No admin password: non-device endpoints fail closed.
            assert client.get("/review").status_code == 503
            # With a password, authentication is required on admin routes.
            os.environ["INKTIME_ADMIN_PASSWORD"] = "test-only-admin-password"
            assert client.get("/review").status_code == 401
            header = "Basic " + base64.b64encode(
                b"admin:test-only-admin-password").decode("ascii")
            # A nonexistent admin URL must reach routing only after auth.
            assert client.get("/__not_found__", headers={"Authorization": header}).status_code == 404
            # Legacy device path remains key-scoped, never admin-authorized.
            assert client.get("/static/inktime/wrong/latest.bin").status_code == 404
            assert client.get("/static/inktime/smoke-device-key/latest.bin").status_code == 404
'''

@unittest.skipUnless(find_spec("flask") and find_spec("PIL"), "Flask/Pillow are needed for integration smoke")
class FlaskStartupSmokeTests(unittest.TestCase):
    def _run_child(self, mode):
        p = subprocess.run([sys.executable, "-c", CHILD, mode], cwd=ROOT,
                           capture_output=True, text=True, timeout=45)
        self.assertEqual(p.returncode, 0, f"mode={mode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")

    def test_current_schema_flask_boot_auth_and_device_routes(self):
        self._run_child("valid")

    def test_missing_database_rejected_without_creating_files(self):
        self._run_child("missing")

    def test_invalid_config_rejected_before_database_and_output(self):
        self._run_child("invalid")


if __name__ == "__main__":
    unittest.main()
