"""R1 Flask-level guard tests for both unified and independent WebUI entrypoints.

Only local synthetic requests are used; no real database or device is accessed.
"""
import base64
import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask
from admin_security import CSRF_COOKIE_NAME, install_admin_security


def basic(username="admin", password="correct-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class AdminGuardFlaskTests(unittest.TestCase):
    def setUp(self):
        self.password_context = patch.dict(os.environ, {
            "INKTIME_ADMIN_USERNAME": "admin",
            "INKTIME_ADMIN_PASSWORD": "correct-password",
        })
        self.password_context.start()

    def tearDown(self):
        self.password_context.stop()

    def make_app(self, *, device=False):
        app = Flask(__name__)
        install_admin_security(app, allow_device_routes=device)
        @app.get("/admin-read")
        def admin_read():
            return "ok"
        @app.post("/admin-write")
        def admin_write():
            return "changed"
        @app.get("/static/inktime/<key>/time")
        def time_route(key):
            return "ok" if key == "device-secret" else ("missing", 404)
        return app

    def test_password_missing_fails_closed(self):
        app = self.make_app()
        with patch.dict(os.environ, {"INKTIME_ADMIN_PASSWORD": ""}):
            with app.test_client() as client:
                response = client.get("/admin-read")
                self.assertEqual(response.status_code, 503)

    def test_authorization_required(self):
        app = self.make_app()
        with app.test_client() as client:
            self.assertEqual(client.get("/admin-read").status_code, 401)
            self.assertEqual(client.get("/admin-read", headers=basic(password="wrong")).status_code, 401)
            response = client.get("/admin-read", headers=basic(), base_url="https://inktime.test")
            self.assertEqual(response.status_code, 200)
            header = response.headers.get("Set-Cookie", "")
            self.assertIn(CSRF_COOKIE_NAME + "=", header)
            self.assertIn("Secure", header)
            self.assertIn("SameSite=Strict", header)

    def test_csrf_enforced_on_flask_route(self):
        app = self.make_app()
        with app.test_client() as client:
            client.get("/admin-read", headers=basic(), base_url="https://inktime.test")
            cookie = client.get_cookie(CSRF_COOKIE_NAME, domain="inktime.test")
            self.assertIsNotNone(cookie)
            self.assertEqual(client.post("/admin-write", headers=basic(), base_url="https://inktime.test").status_code, 403)
            self.assertEqual(client.post("/admin-write", headers={**basic(), "Origin": "https://evil.test",
                "X-InkTime-CSRF": cookie.value}, base_url="https://inktime.test").status_code, 403)
            result = client.post("/admin-write", headers={**basic(),
                "X-InkTime-CSRF": cookie.value, "Origin": "https://inktime.test"},
                base_url="https://inktime.test")
            self.assertEqual(result.status_code, 200)

    def test_device_path_bypass_only_with_explicit_opt_in(self):
        with self.make_app(device=True).test_client() as client:
            self.assertEqual(client.get("/static/inktime/device-secret/time").status_code, 200)
            self.assertEqual(client.get("/static/inktime/wrong/time").status_code, 404)
            self.assertEqual(client.get("/admin-read").status_code, 401)
        with self.make_app(device=False).test_client() as client:
            self.assertEqual(client.get("/static/inktime/device-secret/time").status_code, 401)

    def test_standalone_entrypoint_has_guard(self):
        # Replace unavailable production config with a strictly inert stub.
        cfg = types.ModuleType("config")
        cfg.DB_PATH = ":memory:"
        cfg.IMAGE_DIR = "/nonexistent-photos-ci"
        cfg.DOWNLOAD_KEY = "fixture-only"
        with patch.dict(sys.modules, {"config": cfg}):
            webui = importlib.import_module("webui")
        app = webui.create_standalone_app()
        self.assertFalse(app.debug)
        with app.test_client() as client:
            self.assertEqual(client.get("/api/style_meta").status_code, 401)
            good = client.get("/api/style_meta", headers=basic(),
                              base_url="https://inktime.test")
            self.assertEqual(good.status_code, 200)
            self.assertIn("canvas", good.get_json())
            self.assertEqual(client.post("/api/selftest_report",
                             data="must-not-write", headers=basic(),
                             base_url="https://inktime.test").status_code, 403)
            self.assertEqual(client.get("/api/device_log/fixture-only").status_code, 401)


if __name__ == "__main__":
    unittest.main()
