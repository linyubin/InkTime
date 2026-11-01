"""R1 admin settings page: credential persistence, hot-apply and config.py editing.

v2: full-file introspection (ast+tokenize) with section grouping and comment
help; scalar-only line-span rewrites. Synthetic tests only (temp dirs + Flask
test client); no real config.py or production data is touched.
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
                            introspect_config_file, update_config_file,
                            validate_admin_credentials, write_admin_env_file)


def basic(username="admin", password="correct-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


CONFIG_SAMPLE = """\
# ═══ ① 通用 ═══
# 每日挑选的照片数量
DAILY_PHOTO_QUANTITY = 5  # 渲染与设备端点共用
# 阈值说明
MEMORY_THRESHOLD = 70.0
SCORE_MEMORY_WEIGHT: float = 0.85
ENABLE_REVIEW_WEBUI = True
# API 密钥(ollama 本地服务可任意串)
API_KEY = "ollama"
UNRELATED = "keep me"  # 行尾注释保留
# 复杂配置(只读)
PORTRAIT_BOOST = {"a": 1,
                  "b": 2}

# ═══ ② 路径 ═══
DB_PATH = "./photos.db"
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


class ConfigIntrospectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.py"
        self.config.write_text(CONFIG_SAMPLE, encoding="utf-8")
        self.groups = introspect_config_file(self.config)
        self.items = {i["name"]: i for g in self.groups for i in g["fields"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sections_split_on_header_comments(self):
        titles = [g["title"] for g in self.groups]
        self.assertEqual(titles, ["① 通用", "② 路径"])
        second = {i["name"] for i in self.groups[1]["fields"]}
        self.assertEqual(second, {"DB_PATH"})

    def test_scalars_classified_with_help_and_value(self):
        item = self.items["DAILY_PHOTO_QUANTITY"]
        self.assertEqual((item["kind"], item["value"], item["editable"]), ("int", 5, True))
        self.assertIn("每日挑选的照片数量", item["help"])
        self.assertIn("渲染与设备端点共用", item["inline_help"])
        self.assertEqual(self.items["SCORE_MEMORY_WEIGHT"]["kind"], "float")
        self.assertEqual(self.items["ENABLE_REVIEW_WEBUI"]["kind"], "bool")
        self.assertTrue(self.items["ENABLE_REVIEW_WEBUI"]["value"])

    def test_annotated_assignment_parsed(self):
        item = self.items["SCORE_MEMORY_WEIGHT"]
        self.assertEqual(item["value"], 0.85)
        self.assertEqual(item["literal"], "0.85")

    def test_secrets_flagged_and_complex_readonly(self):
        self.assertTrue(self.items["API_KEY"]["secret"])
        self.assertTrue(self.items["API_KEY"]["editable"])
        boost = self.items["PORTRAIT_BOOST"]
        self.assertEqual(boost["kind"], "complex")
        self.assertFalse(boost["editable"])

    def test_missing_help_falls_back(self):
        self.assertEqual(self.items["UNRELATED"]["help"], "")
        self.assertEqual(self.items["UNRELATED"]["inline_help"], "行尾注释保留")


class ConfigFileRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.py"
        self.config.write_text(CONFIG_SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewrite_scalar_span_preserves_context(self):
        backup = update_config_file(self.config, {
            "DAILY_PHOTO_QUANTITY": 8,
            "SCORE_MEMORY_WEIGHT": 0.9,
            "ENABLE_REVIEW_WEBUI": False,
            "API_KEY": "new-key",
        })
        new_text = self.config.read_text(encoding="utf-8")
        self.assertIn("DAILY_PHOTO_QUANTITY = 8  # 渲染与设备端点共用", new_text)
        self.assertIn("SCORE_MEMORY_WEIGHT: float = 0.9", new_text)
        self.assertIn("ENABLE_REVIEW_WEBUI = False", new_text)
        self.assertIn('API_KEY = "new-key"', new_text)
        self.assertIn('UNRELATED = "keep me"', new_text)
        self.assertIn("# 复杂配置(只读)", new_text)
        self.assertIn('DB_PATH = "./photos.db"', new_text)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), CONFIG_SAMPLE)
        ast.parse(new_text)

    def test_float_literal_is_valid_python(self):
        update_config_file(self.config, {"MEMORY_THRESHOLD": 75})
        self.assertIn("MEMORY_THRESHOLD = 75.0", self.config.read_text(encoding="utf-8"))

    def test_rejects_nonexistent_key(self):
        with self.assertRaises(SettingsError):
            update_config_file(self.config, {"NOT_IN_FILE": 1})

    def test_rejects_complex_and_multiline_targets(self):
        with self.assertRaises(SettingsError):
            update_config_file(self.config, {"PORTRAIT_BOOST": {"a": 2}})

    def test_rejects_type_mismatches(self):
        for updates in (
            {"DAILY_PHOTO_QUANTITY": "five"},
            {"DAILY_PHOTO_QUANTITY": 5.5},
            {"DAILY_PHOTO_QUANTITY": True},
            {"ENABLE_REVIEW_WEBUI": "maybe"},
            {"API_KEY": "a\nb"},
        ):
            with self.subTest(updates=updates), self.assertRaises(SettingsError):
                update_config_file(self.config, updates)

    def test_empty_secret_update_is_noop(self):
        before = self.config.read_text(encoding="utf-8")
        update_config_file(self.config, {"API_KEY": ""})
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_never_writes_invalid_python(self):
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(SettingsError):
            update_config_file(self.config, {"DAILY_PHOTO_QUANTITY": "import os"})
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_empty_updates_rejected(self):
        with self.assertRaises(SettingsError):
            update_config_file(self.config, {})


def build_settings_app(env_file: Path, config_path: Path, *, popen=None,
                       allow_restart: str = "1"):
    app = Flask(__name__)
    install_admin_security(app)

    @app.get("/settings")
    def settings_page():
        return "settings-page"

    @app.get("/api/settings/ping")
    def ping():
        return jsonify({"ok": True})

    @app.post("/api/settings/restart")
    def restart():
        import subprocess as _sp
        if os.environ.get("INKTIME_ALLOW_SERVICE_RESTART") != allow_restart:
            return jsonify({"ok": False, "error": "restart disabled"}), 400
        fn = popen or (lambda cmd, **kw: _sp.Popen(cmd, **kw))
        fn(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart",
               "inktime-server.service"],
              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True)
        return jsonify({"ok": True, "restarting": True})

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

    def _csrf_cookie(self, client) -> str:
        first = client.get("/settings", headers=basic())
        cookie = first.headers.get("Set-Cookie", "").split(";", 1)[0]
        self.assertTrue(cookie.startswith(CSRF_COOKIE_NAME))
        return cookie

    def test_get_requires_basic_auth(self):
        with self.app.test_client() as client:
            self.assertEqual(client.get("/settings").status_code, 401)
            self.assertEqual(client.get("/settings", headers=basic()).status_code, 200)

    def test_post_rejects_missing_csrf(self):
        with self.app.test_client() as client:
            response = client.post("/api/settings/credentials", json={
                "username": "andy", "password": "new-pass-123"},
                headers=basic())
            self.assertEqual(response.status_code, 403)
            self.assertFalse(self.env_file.exists())

    def test_post_with_csrf_rotates_credentials(self):
        with self.app.test_client() as client:
            cookie = self._csrf_cookie(client)
            response = client.post(
                "/api/settings/credentials",
                json={"username": "rotated", "password": "new-pass-123"},
                headers={**basic(), "X-InkTime-CSRF": cookie.split("=", 1)[1],
                         "Cookie": cookie})
            self.assertEqual(response.status_code, 200)
            content = self.env_file.read_text(encoding="utf-8")
            self.assertIn("INKTIME_ADMIN_USERNAME=rotated", content)
            self.assertNotIn("correct-password", content)

    def test_post_config_requires_csrf_and_writes_on_success(self):
        with self.app.test_client() as client:
            response = client.post("/api/settings/config", json={"DAILY_PHOTO_QUANTITY": 6},
                                   headers=basic())
            self.assertEqual(response.status_code, 403)
            cookie = self._csrf_cookie(client)
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
            cookie = self._csrf_cookie(client)
            response = client.post(
                "/api/settings/credentials",
                json={"username": "only-user"},
                headers={**basic(), "X-InkTime-CSRF": cookie.split("=", 1)[1],
                         "Cookie": cookie})
            self.assertEqual(response.status_code, 200)
            content = self.env_file.read_text(encoding="utf-8")
            self.assertIn("INKTIME_ADMIN_USERNAME=only-user", content)
            self.assertIn("INKTIME_ADMIN_PASSWORD=correct-password", content)

    def test_ping_requires_auth(self):
        with self.app.test_client() as client:
            self.assertEqual(client.get("/api/settings/ping").status_code, 401)
            self.assertEqual(
                client.get("/api/settings/ping", headers=basic()).status_code, 200)

    def test_restart_disabled_without_flag(self):
        app = build_settings_app(self.env_file, self.config_path, allow_restart="0")
        with app.test_client() as client:
            cookie = self._csrf_cookie(client)
            response = client.post(
                "/api/settings/restart", json={},
                headers={**basic(), "X-InkTime-CSRF": cookie.split("=", 1)[1],
                         "Cookie": cookie})
            self.assertEqual(response.status_code, 400)

    def test_restart_invokes_systemctl_when_enabled(self):
        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return None

        app = build_settings_app(self.env_file, self.config_path, popen=fake_popen)
        with patch.dict(os.environ, {"INKTIME_ALLOW_SERVICE_RESTART": "1"}):
            with app.test_client() as client:
                cookie = self._csrf_cookie(client)
                response = client.post(
                    "/api/settings/restart", json={},
                    headers={**basic(), "X-InkTime-CSRF": cookie.split("=", 1)[1],
                             "Cookie": cookie})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(calls), 1)
                cmd, kwargs = calls[0]
                self.assertEqual(cmd, ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "inktime-server.service"])
                self.assertTrue(kwargs.get("start_new_session"))


if __name__ == "__main__":
    unittest.main()
