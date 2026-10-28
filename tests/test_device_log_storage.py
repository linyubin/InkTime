"""R1/PR-02: device journal filenames never persist plaintext bearer keys."""
import ast
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from flask import Flask, abort, request

from device_log_storage import device_log_paths, append_private_device_log
from download_key_security import authorized_download_key, resolve_download_keys

ROOT = Path(__file__).resolve().parents[1]


def extract_function(filename, name, namespace):
    path = ROOT / filename
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                         str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError("missing function " + name)


class DeviceLogFilenameTests(unittest.TestCase):
    def test_opaque_path_is_stable_distinct_and_does_not_embed_key(self):
        with TemporaryDirectory() as d:
            legacy, new = device_log_paths(Path(d), "legacySecret")
            self.assertEqual(legacy.name, "legacySecret.log")
            self.assertRegex(new.name, r"^device-[a-f0-9]{64}\.log$")
            self.assertNotIn("legacySecret", new.name)
            self.assertEqual((legacy, new), device_log_paths(Path(d), "legacySecret"))
            self.assertNotEqual(new, device_log_paths(Path(d), "otherSecret")[1])

    def test_untrusted_key_cannot_escape_directory(self):
        with TemporaryDirectory() as d:
            for key in ("", "../escape", "a/b", "a.b", "bad\nname", "密钥", "x" * 129, None):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    device_log_paths(Path(d), key)

    def test_actual_device_upload_reads_legacy_cursor_but_writes_opaque_file(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            legacy, opaque = device_log_paths(directory, "legacySecret")
            legacy.write_text('{"ls":8,"t":"OLD"}\n', encoding="utf-8")
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024 * 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "append_private_device_log": append_private_device_log,
                "DOWNLOAD_KEYS": ("legacySecret",),
                "authorized_download_key": authorized_download_key,
                "json": json, "datetime": datetime, "request": request, "abort": abort,
                "Path": Path,
            }
            extract_function("server.py", "_devlog_path", ns)
            extract_function("server.py", "_devlog_load_max_ls", ns)
            endpoint = extract_function("server.py", "esp_device_log", ns)
            app = Flask(__name__)

            @app.post("/log/<key>")
            def upload(key):
                return endpoint(key)

            payload = '{"ls":8,"t":"DUP"}\n{"ls":9,"t":"NEW"}\n'
            with app.test_client() as client:
                self.assertEqual(client.post("/log/wrong", data=payload).status_code, 404)
                r = client.post("/log/legacySecret", data=payload)
                self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "OK n=1"))
                self.assertTrue(opaque.exists())
                self.assertEqual(opaque.stat().st_mode & 0o777, 0o600)
                self.assertEqual(legacy.read_text(encoding="utf-8"), '{"ls":8,"t":"OLD"}\n')
                self.assertEqual([json.loads(x)["ls"] for x in opaque.read_text().splitlines()], [9])
                self.assertEqual(client.post("/log/legacySecret", data=payload).get_data(as_text=True), "OK n=0")
                ns["_devlog_max_ls"].clear()  # simulated server restart
                self.assertEqual(client.post("/log/legacySecret", data=payload).get_data(as_text=True), "OK n=0")

    def test_webui_reads_legacy_and_opaque_logs_across_key_rotation(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            old_legacy, _ = device_log_paths(directory, "oldKey")
            _, new_opaque = device_log_paths(directory, "newKey")
            old_legacy.write_text('{"ls":1,"t":"OLD","b":1,"ms":0}\n', encoding="utf-8")
            new_opaque.write_text('{"ls":2,"t":"NEW","b":1,"ms":1}\n', encoding="utf-8")
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVLOG_PAGE_SIZE": 500,
                "cfg": SimpleNamespace(DOWNLOAD_KEY="oldKey"),
                "os": SimpleNamespace(environ={
                    "INKTIME_DOWNLOAD_KEY": "newKey",
                    "INKTIME_DOWNLOAD_KEY_PREVIOUS": "oldKey",
                }),
                "device_log_paths": device_log_paths,
                "resolve_download_keys": resolve_download_keys,
                "json": json, "datetime": datetime,
                "_ANCHOR_RE": re.compile(r" anchor=(\d+)"),
                "render_template": lambda _name, **kw: kw,
            }
            result = extract_function("webui.py", "devlog", ns)()
            self.assertTrue(result["exists"])
            self.assertEqual([x["t"] for x in result["rows"]], ["NEW", "OLD"])
            ns["os"].environ.pop("INKTIME_DOWNLOAD_KEY_PREVIOUS")
            result = ns["devlog"]()
            self.assertEqual([x["t"] for x in result["rows"]], ["NEW"])


    def test_new_log_is_private_even_with_permissive_umask(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d) / "device", "testKey")[1]
            previous = os.umask(0)
            try:
                append_private_device_log(path, ['{"ls":1}'], max_bytes=100)
            finally:
                os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.read_text(), '{"ls":1}\n')

    def test_existing_log_permissions_tightened_without_losing_data(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            path.write_text("old\n", encoding="utf-8")
            path.chmod(0o644)
            append_private_device_log(path, ["new"], max_bytes=100)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), "old\nnew\n")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"),
                         "requires no-follow file opening and symlinks")
    def test_symlink_and_hardlink_targets_fail_without_modifying_victim(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            path = device_log_paths(directory, "testKey")[1]
            victim = directory / "victim.txt"
            victim.write_text("untouched", encoding="utf-8")
            path.symlink_to(victim)
            with self.assertRaises(OSError):
                append_private_device_log(path, ["attack"], max_bytes=100)
            self.assertEqual(victim.read_text(), "untouched")
            path.unlink()
            os.link(victim, path)
            with self.assertRaises(OSError):
                append_private_device_log(path, ["attack"], max_bytes=100)
            self.assertEqual(victim.read_text(), "untouched")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"),
                         "requires no-follow file opening and symlinks")
    def test_rotation_preserves_private_files_and_rejects_symlink_generation(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            append_private_device_log(path, ["first"], max_bytes=1)
            append_private_device_log(path, ["second"], max_bytes=1)
            rotated = path.with_suffix(".log.1")
            self.assertEqual(rotated.read_text(), "first\n")
            self.assertEqual(path.read_text(), "second\n")
            self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            rotated.unlink()
            victim = Path(d) / "victim.txt"
            victim.write_text("safe", encoding="utf-8")
            rotated.symlink_to(victim)
            with self.assertRaises(OSError):
                append_private_device_log(path, ["third"], max_bytes=1)
            self.assertEqual(victim.read_text(), "safe")
            self.assertEqual(path.read_text(), "second\n")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"),
                         "requires no-follow file opening and symlinks")
    def test_symlink_log_directory_is_rejected(self):
        with TemporaryDirectory() as d:
            target = Path(d) / "outside"
            target.mkdir()
            link = Path(d) / "device"
            link.symlink_to(target, target_is_directory=True)
            path = device_log_paths(link, "testKey")[1]
            with self.assertRaises(OSError):
                append_private_device_log(path, ["no"], max_bytes=100)
            self.assertEqual(list(target.iterdir()), [])



if __name__ == "__main__":
    unittest.main()
