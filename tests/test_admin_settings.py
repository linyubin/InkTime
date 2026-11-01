"""R1 admin settings page: credential persistence, hot-apply and config.py rewrite.

Synthetic tests only (temp dirs + Flask test client); no real config.py or
production data is touched.
"""
import ast
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify
from admin_security import CSRF_COOKIE_NAME, install_admin_security
import admin_settings
from admin_settings import (SettingsError, apply_admin_credentials,
                            update_config_file, validate_admin_credentials,
                            write_admin_env_file)


def basic(username="admin", password="correct-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


CONFIG_SAMPLE = """\
# comment preserved
DAILY_PHOTO_QUANTITY = 5
MEMORY_THRESHOLD = 70.0
SCORE_MEMORY_WEIGHT: float = 0.85
ESP32_SERVE_WAIT_MIN = 15
ENABLE_REVIEW_WEBUI = True
UNRELATED = "keep me"
"""


class AdminCredentialTests(unittest.TestCase):
    def test_validate_rules(self):
        self.assertEqual(validate_admin_credentials("andy", "longenough1"), ("andy", "longenough1"))
        for username in ("", "a b", "x" * 65, "用户"):
            with self.subTest(username=username), self.assertRaises(SettingsError):
                validate_admin_credentials(username, "longenough1")
        for password in ("", "short", "x" * 129):
            with self.subTest(password=password), self.assertRaises(SettingsError):
                validate_admin_credentials("andy", password)

    def test_env_file_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "admin.env"
            write_admin_env_file(env_path, "andy", "s3cret-pass")
            mode = env_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            lines = dict(
                line.split("=", 1) for line in env_path.read_text().strip().splitlines()
            )
            self.assertEqual(lines["INKTIME_ADMIN_USERNAME"], "andy")
            self.assertEqual(lines["INKTIME_ADMIN_PASSWORD"], "s3cret-pass")

    def test_env_file_rejected_input_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "admin.env"
            write_admin_env_file(env_path, "andy", "s3cret-pass")
            before = env_path.read_bytes()
            with self.assertRaises(SettingsError):
                write_admin_env_file(env_path, "andy", "short")
            self.assertEqual(env_path.read_bytes(), before)

    def test_apply_updates_process_environment(self):
        with patch.dict(os.environ, {}, clear=False):
            apply_admin_credentials("rotated-user", "rotated-pass-1")
            self.assertEqual(os.environ["INKTIME_ADMIN_USERNAME"], "rotated-user")
            self.assertEqual(os.environ["INKTIME_ADMIN_PASSWORD"], "rotated-pass-1")


class ConfigFileRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.py"
        self.config.write_text(CONFIG_SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewrite_replaces_values_and_keeps_rest(self):
        backup = update_config_file(self.config, {
            "DAILY_PHOTO_QUANTITY": 8,
            "SCORE_MEMORY_WEIGHT": 0.9,
            "ENABLE_REVIEW_WEBUI": False,
        })
        new_text = self.config.read_text(encoding="utf-8")
        self.assertIn("DAILY_PHOTO_QUANTITY = 8", new_text)
        self.assertIn("SCORE_MEMORY_WEIGHT: float = 0.9", new_text)
        self.assertIn("ENABLE_REVIEW_WEBUI = False", new_text)
        self.assertIn('UNRELATED = "keep me"', new_text)
        self.assertIn("# comment preserved", new_text)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), CONFIG_SAMPLE)
        ast.parse(new_text)  # result stays valid Python

    def test_missing_key_is_appended(self):
        self.config.write_text("A = 1\n", encoding="utf-8")
        update_config_file(self.config, {"DAILY_PHOTO_QUANTITY": 3})
        self.assertIn("DAILY_PHOTO_QUANTITY = 3", self.config.read_text(encoding="utf-8"))

    def test_rejects_unknown_key_and_bad_values(self):
        for updates in (
            {"NOT_ALLOWED": 1},
            {"DAILY_PHOTO_QUANTITY": 0},
            {"DAILY_PHOTO_QUANTITY": 99},
            {"DAILY_PHOTO_QUANTITY": "five"},
            {"SCORE_MEMORY_WEIGHT": 1.5},
            {"ENABLE_REVIEW_WEBUI": "maybe"},
        ):
            with self.subTest(updates=updates), self.assertRaises(SettingsError):
                update_config_file(self.config, updates)

    def test_never_writes_invalid_python(self):
        # a value that would break the file must fail validation, not truncate
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(SettingsError):
            update_config_file(self.config, {"DAILY_PHOTO_QUANTITY": "import os"})
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)


def build_settings_app(env_file: Path, config_path: Path):
    app = Flask(__name__)
    install_admin_security(app)

    @app.get("/settings")
    def settings_page():
        return "settings-page"

    @app.post("/api/settings/credentials")
    def credentials():
        data = __import__("flask").request.get_json(silent=True) or {}
        try:
            username = str(data.get("username", ""))
            password = str(data.get("password", ""))
            if not password:
                password = os.environ.get("INKTIME_ADMIN_PASSWORD", "")
                if not password:
                    return jsonify({"ok": False, "error": "首次必须填写新密码"}), 400
            write_admin_env_file(env_file, username, password)
            apply_admin_credentials(username, password)
        except SettingsError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "relogin": True})

    @app.post("/api/settings/config")
    def config_route():
        data = __import__("flask").request.get_json(silent=True) or {}
        try:
            update_config_file(config_path, data)
        except SettingsError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "restart_required": True})

    return app


class SettingsRouteGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.tmp.name) / "admin.env"
        self.config_path = Path(self.tmp.name) / "config.py"
        self.config_path.write_text(CONFIG_SAMPLE, encoding="utf-8")
        self.env_context = patch.dict(os.environ, {
            "INKTIME_ADMIN_USERNAME": "admin",
            "INKTIME_ADMIN_PASSWORD": "correct-password",
        })
        self.env_context.start()
        self.app = build_settings_app(self.env_file, self.config_path)

    def tearDown(self):
        self.env_context.stop()
        self.tmp.cleanup()

    def test_get_requires_basic_auth(self):
        with self.app.test_client() as client:
            self.assertEqual(client.get("/settings").status_code, 401)
            response = client.get("/settings", headers=basic())
            self.assertEqual(response.status_code, 200)

    def test_post_rejects_missing_csrf(self):
        with self.app.test_client() as client:
            response = client.post("/api/settings/credentials", json={
                "username": "andy", "password": "new-pass-123"},
                headers=basic())
            self.assertEqual(response.status_code, 403)
            self.assertFalse(self.env_file.exists())

    def test_post_with_csrf_rotates_credentials(self):
        with self.app.test_client() as client:
            first = client.get("/settings", headers=basic())
            cookie = first.headers.get("Set-Cookie", "").split(";", 1)[0]
            name, _, token = cookie.partition("=")
            self.assertEqual(name, CSRF_COOKIE_NAME)
            response = client.post(
                "/api/settings/credentials",
                json={"username": "rotated", "password": "new-pass-123"},
                headers={**basic(), "X-InkTime-CSRF": token, "Cookie": cookie})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(self.env_file.exists())
            self.assertIn("INKTIME_ADMIN_USERNAME=rotated",
                          self.env_file.read_text(encoding="utf-8"))
            # password rotation invalidates the old token
            self.assertNotIn("correct-password", self.env_file.read_text(encoding="utf-8"))

    def test_post_config_requires_csrf_and_writes_on_success(self):
        with self.app.test_client() as client:
            response = client.post("/api/settings/config", json={"DAILY_PHOTO_QUANTITY": 6},
                                   headers=basic())
            self.assertEqual(response.status_code, 403)
            first = client.get("/settings", headers=basic())
            cookie = first.headers.get("Set-Cookie", "").split(";", 1)[0]
            self.assertTrue(cookie.startswith(CSRF_COOKIE_NAME))
            response = client.post(
                "/api/settings/config",
                json={"DAILY_PHOTO_QUANTITY": 6},
                headers={**basic(), "X-InkTime-CSRF": cookie.split("=", 1)[1],
                         "Cookie": cookie})
            self.assertEqual(response.status_code, 200)
            self.assertIn("DAILY_PHOTO_QUANTITY = 6",
                          self.config_path.read_text(encoding="utf-8"))

    def test_empty_password_keeps_existing(self):
        with self.app.test_client() as client:
            first = client.get("/settings", headers=basic())
            cookie = first.headers.get("Set-Cookie", "").split(";", 1)[0]
            token = cookie.split("=", 1)[1]
            response = client.post(
                "/api/settings/credentials",
                json={"username": "only-user"},
                headers={**basic(), "X-InkTime-CSRF": token, "Cookie": cookie})
            self.assertEqual(response.status_code, 200)
            content = self.env_file.read_text(encoding="utf-8")
            self.assertIn("INKTIME_ADMIN_USERNAME=only-user", content)
            self.assertIn("INKTIME_ADMIN_PASSWORD=correct-password", content)


if __name__ == "__main__":
    unittest.main()
