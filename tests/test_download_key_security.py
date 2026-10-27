"""R1 download-key rotation: pure helper and isolated Flask route tests."""
import ast
import copy
import time
import unittest
from pathlib import Path
from flask import Flask, abort
from download_key_security import authorized_download_key, resolve_download_keys

SERVER = Path(__file__).resolve().parents[1] / "server.py"
PROTECTED_ROUTES = {
    "esp_photo", "esp_latest", "esp_photo_sidecar", "esp_latest_sidecar",
    "esp_calib_portrait", "esp_calib_landscape", "esp_preview", "esp_time",
    "esp_device_log", "esp_device_register",
}

def extract_function(name, namespace):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                         str(SERVER), "exec"), namespace)
            return namespace[name]
    raise AssertionError("missing function: " + name)

class DownloadKeySecurityTests(unittest.TestCase):
    def test_legacy_config_unchanged(self):
        self.assertEqual(resolve_download_keys("existingKey", {}), ("existingKey",))

    def test_env_override_does_not_auto_accept_old_key(self):
        keys = resolve_download_keys("oldKey", {"INKTIME_DOWNLOAD_KEY": "newKey"})
        self.assertEqual(keys, ("newKey",))
        self.assertFalse(authorized_download_key("oldKey", keys))

    def test_explicit_previous_key_and_revocation(self):
        keys = resolve_download_keys("oldKey", {
            "INKTIME_DOWNLOAD_KEY": "newKey",
            "INKTIME_DOWNLOAD_KEY_PREVIOUS": "oldKey",
        })
        self.assertEqual(keys, ("newKey", "oldKey"))
        for candidate in ("newKey", "oldKey"):
            self.assertTrue(authorized_download_key(candidate, keys))
        for candidate in ("badKey", "newKey/extra", "", None):
            self.assertFalse(authorized_download_key(candidate, keys))
        self.assertFalse(authorized_download_key("oldKey", ("newKey",)))

    def test_invalid_active_key_fails_closed(self):
        for value in ("", " ", "a/b", "a?b", "a%2Fb", "a.b", "x" * 129, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_download_keys(value, {})
        with self.assertRaises(ValueError):
            resolve_download_keys("validKey", {"INKTIME_DOWNLOAD_KEY": ""})

    def test_invalid_previous_key_fails_closed(self):
        for value in ("a/b", " a", "a?b", "x" * 129, "newKey"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_download_keys("newKey", {"INKTIME_DOWNLOAD_KEY_PREVIOUS": value})

    def test_all_device_handlers_use_rotation_validator(self):
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        seen = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in PROTECTED_ROUTES:
                seen.add(node.name)
                self.assertIn("authorized_download_key(key, DOWNLOAD_KEYS)", ast.unparse(node), node.name)
                self.assertNotIn("key != DOWNLOAD_KEY", ast.unparse(node), node.name)
        self.assertEqual(seen, PROTECTED_ROUTES)

    def test_actual_time_route_accepts_then_revokes_previous_key(self):
        ns = {"DOWNLOAD_KEYS": ("newKey", "oldKey"),
              "authorized_download_key": authorized_download_key,
              "abort": abort, "time": time}
        endpoint = extract_function("esp_time", ns)
        app = Flask(__name__)
        @app.get("/time/<key>")
        def check(key):
            return endpoint(key)
        with app.test_client() as client:
            for key in ("newKey", "oldKey"):
                self.assertEqual(client.get("/time/" + key).status_code, 200)
            self.assertEqual(client.get("/time/badKey").status_code, 404)
            ns["DOWNLOAD_KEYS"] = ("newKey",)
            self.assertEqual(client.get("/time/oldKey").status_code, 404)
            self.assertEqual(client.get("/time/newKey").status_code, 200)

if __name__ == "__main__":
    unittest.main()
