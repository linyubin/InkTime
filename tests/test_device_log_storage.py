"""R1/PR-02: device journal filenames never persist plaintext bearer keys."""
import ast
import copy
from datetime import datetime
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
from unittest.mock import patch
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from flask import Flask, abort, request

from device_log_storage import (device_log_paths, device_log_lock, append_private_device_log,
                                read_device_log_lines, recover_incomplete_private_device_log_tail)
from download_key_security import authorized_download_key, resolve_download_keys

ROOT = Path(__file__).resolve().parents[1]


def _hold_lock_in_child(path, entered, release):
    with device_log_lock(path):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("lock test timed out")




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
                "device_log_lock": device_log_lock,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
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
                ns["_devlog_max_ls"]["legacySecret"] = 0  # stale worker cache must be ignored
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
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
                "resolve_download_keys": resolve_download_keys,
                "abort": abort,
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



    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_private_lock_persists_and_rejects_symlink_or_hardlink(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            lock_path = path.with_name(path.name + ".lock")
            with device_log_lock(path):
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(lock_path.exists())  # never unlink: inode identity matters
            lock_path.unlink()
            victim = Path(d) / "victim.txt"
            victim.write_text("untouched", encoding="utf-8")
            lock_path.symlink_to(victim)
            with self.assertRaises(OSError), device_log_lock(path):
                pass
            lock_path.unlink()
            os.link(victim, lock_path)
            with self.assertRaises(OSError), device_log_lock(path):
                pass
            self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods()
                         and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX fork and flock")
    def test_lock_serializes_separate_processes(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            ctx = multiprocessing.get_context("fork")
            first_entered, first_release = ctx.Event(), ctx.Event()
            second_entered, second_release = ctx.Event(), ctx.Event()
            first = ctx.Process(target=_hold_lock_in_child,
                                args=(path, first_entered, first_release))
            second = ctx.Process(target=_hold_lock_in_child,
                                 args=(path, second_entered, second_release))
            try:
                first.start()
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertFalse(second_entered.wait(0.25))
                first_release.set()
                self.assertTrue(second_entered.wait(5))
            finally:
                first_release.set()
                second_release.set()
                for process in (first, second):
                    if process.pid is not None:
                        process.join(5)
                        if process.is_alive():
                            process.terminate()
                            process.join(5)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

    def test_actual_upload_recovers_rotated_only_cursor_after_crash(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            _, opaque = device_log_paths(directory, "legacySecret")
            rotated = opaque.with_suffix(".log.1")
            rotated.write_text('{"ls":40,"t":"before-crash"}\n', encoding="utf-8")
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {"legacySecret": 0},
                "device_log_paths": device_log_paths,
                "device_log_lock": device_log_lock,
                "append_private_device_log": append_private_device_log,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
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

            with app.test_client() as client:
                r = client.post("/log/legacySecret", data='{"ls":40}\n{"ls":41}\n')
            self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "OK n=1"))
            self.assertEqual([json.loads(x)["ls"] for x in opaque.read_text().splitlines()], [41])
            self.assertEqual(rotated.read_text(), '{"ls":40,"t":"before-crash"}\n')


    def test_safe_reader_accepts_legacy_and_opaque_files_without_writing(self):
        with TemporaryDirectory() as d:
            directory = Path(d) / "device"
            self.assertIsNone(read_device_log_lines(directory / "missing.log"))
            directory.mkdir()
            legacy, opaque = device_log_paths(directory, "testKey")
            legacy.write_text('{"ls":1}\n', encoding="utf-8")
            opaque.write_text('{"ls":2}\n', encoding="utf-8")
            self.assertEqual(read_device_log_lines(legacy), ['{"ls":1}'])
            self.assertEqual(read_device_log_lines(opaque), ['{"ls":2}'])
            self.assertEqual(legacy.read_text(), '{"ls":1}\n')

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_safe_reader_rejects_symlinks_hardlinks_and_oversized_files(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            victim = directory / "victim.txt"
            victim.write_text('{"ls":999}\n', encoding="utf-8")
            legacy = device_log_paths(directory, "testKey")[0]
            legacy.symlink_to(victim)
            with self.assertRaises(OSError):
                read_device_log_lines(legacy)
            legacy.unlink()
            os.link(victim, legacy)
            with self.assertRaises(OSError):
                read_device_log_lines(legacy)
            legacy.unlink()
            legacy.write_text("12345", encoding="utf-8")
            with self.assertRaises(OSError):
                read_device_log_lines(legacy, max_bytes=4)
            self.assertEqual(victim.read_text(), '{"ls":999}\n')

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_safe_reader_rejects_symlinked_log_directory(self):
        with TemporaryDirectory() as d:
            outside = Path(d) / "outside"
            outside.mkdir()
            (outside / "legacy.log").write_text("private", encoding="utf-8")
            link = Path(d) / "device"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                read_device_log_lines(link / "legacy.log")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_actual_upload_refuses_unsafe_legacy_cursor_recovery(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            legacy, opaque = device_log_paths(directory, "legacySecret")
            victim = directory / "victim.txt"
            victim.write_text('{"ls":900}\n', encoding="utf-8")
            legacy.symlink_to(victim)
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
                "append_private_device_log": append_private_device_log,
                "device_log_lock": device_log_lock,
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
            with app.test_client() as client:
                r = client.post("/log/legacySecret", data='{"ls":1}\n')
            self.assertEqual(r.status_code, 500)
            self.assertFalse(opaque.exists())
            self.assertEqual(ns["_devlog_max_ls"], {})
            self.assertEqual(victim.read_text(), '{"ls":900}\n')

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_actual_webui_refuses_symlinked_historical_journal(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            legacy = device_log_paths(directory, "oldKey")[0]
            victim = directory / "victim.txt"
            victim.write_text('{"t":"SECRET"}\n', encoding="utf-8")
            legacy.symlink_to(victim)
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVLOG_PAGE_SIZE": 500,
                "cfg": SimpleNamespace(DOWNLOAD_KEY="oldKey"),
                "os": SimpleNamespace(environ={}),
                "device_log_paths": device_log_paths,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
                "resolve_download_keys": resolve_download_keys,
                "abort": abort, "json": json, "datetime": datetime,
                "_ANCHOR_RE": re.compile(r" anchor=(\d+)"),
                "render_template": lambda _name, **kw: kw,
            }
            endpoint = extract_function("webui.py", "devlog", ns)
            app = Flask(__name__)
            with app.test_request_context("/devlog"):
                with self.assertRaises(Exception) as err:
                    endpoint()
            self.assertEqual(getattr(err.exception, "code", None), 503)
            self.assertEqual(victim.read_text(), '{"t":"SECRET"}\n')


    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX directory fsync")
    def test_file_and_directory_fsync_before_success_and_duplicate_ack(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            real_fsync = os.fsync
            calls = []

            def trace(fd):
                mode = os.fstat(fd).st_mode
                calls.append("file" if stat.S_ISREG(mode) else "directory")
                return real_fsync(fd)

            with patch("device_log_storage.os.fsync", side_effect=trace):
                append_private_device_log(path, ['{"ls":1}'], max_bytes=100)
            self.assertEqual(calls[-2:], ["file", "directory"])
            calls.clear()
            with patch("device_log_storage.os.fsync", side_effect=trace):
                append_private_device_log(path, [], max_bytes=100)
            self.assertEqual(calls[-2:], ["file", "directory"])
            self.assertEqual(path.read_text(), '{"ls":1}\n')

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX directory fsync")
    def test_empty_retry_does_not_create_a_missing_journal(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            append_private_device_log(path, [], max_bytes=100)
            self.assertFalse(path.exists())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX directory fsync")
    def test_directory_fsync_failure_is_not_silently_acknowledged(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            real_fsync = os.fsync

            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("simulated directory sync failure")
                return real_fsync(fd)

            with patch("device_log_storage.os.fsync", side_effect=fail_directory):
                with self.assertRaises(OSError):
                    append_private_device_log(path, ['{"ls":1}'], max_bytes=100)
            # The append may be visible even though the API must return 500.
            self.assertEqual(path.read_text(), '{"ls":1}\n')
            append_private_device_log(path, [], max_bytes=100)
            self.assertEqual(path.read_text(), '{"ls":1}\n')

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX directory fsync")
    def test_flask_fsync_failure_returns_500_then_retry_syncs_duplicate(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            _, opaque = device_log_paths(directory, "legacySecret")
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "device_log_lock": device_log_lock,
                "append_private_device_log": append_private_device_log,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
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

            payload = '{"ls":7,"t":"RETRY"}\n'
            with app.test_client() as client:
                with patch("device_log_storage.os.fsync",
                           side_effect=OSError("simulated fsync failure")):
                    failed = client.post("/log/legacySecret", data=payload)
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(ns["_devlog_max_ls"], {})
                self.assertEqual([json.loads(x)["ls"] for x in opaque.read_text().splitlines()], [7])
                recovered = client.post("/log/legacySecret", data=payload)
                self.assertEqual((recovered.status_code, recovered.get_data(as_text=True)),
                                 (200, "OK n=0"))
                self.assertEqual(ns["_devlog_max_ls"]["legacySecret"], 7)
                with patch("device_log_storage.os.fsync",
                           side_effect=OSError("simulated duplicate sync failure")):
                    duplicate_failed = client.post("/log/legacySecret", data=payload)
                self.assertEqual(duplicate_failed.status_code, 500)


    @unittest.skipUnless(hasattr(os, "pread") and hasattr(os, "O_NOFOLLOW"),
                         "requires POSIX no-follow recovery")
    def test_recovery_truncates_only_unterminated_tail_and_is_idempotent(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            path.write_bytes(b'{"ls":1}\n{"ls":2')
            with device_log_lock(path):
                self.assertEqual(recover_incomplete_private_device_log_tail(path), len(b'{"ls":2'))
                self.assertEqual(recover_incomplete_private_device_log_tail(path), 0)
            self.assertEqual(path.read_bytes(), b'{"ls":1}\n')
            self.assertEqual(recover_incomplete_private_device_log_tail(path), 0)
            path.write_bytes(b'{"ls":3}')
            self.assertEqual(recover_incomplete_private_device_log_tail(path), len(b'{"ls":3}'))
            self.assertEqual(path.read_bytes(), b"")

    @unittest.skipUnless(hasattr(os, "pread") and hasattr(os, "O_NOFOLLOW"),
                         "requires POSIX no-follow recovery")
    def test_recovery_missing_complete_and_oversized_tail_fail_safe(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            self.assertEqual(recover_incomplete_private_device_log_tail(path), 0)
            self.assertFalse(path.exists())
            path.write_bytes(b'{"ls":1}\n')
            self.assertEqual(recover_incomplete_private_device_log_tail(path), 0)
            payload = b'{"ls":1}\n' + b"x" * 32
            path.write_bytes(payload)
            with self.assertRaisesRegex(OSError, "exceeds safe recovery limit"):
                recover_incomplete_private_device_log_tail(path, max_tail_bytes=16)
            self.assertEqual(path.read_bytes(), payload)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"),
                         "requires no-follow file opening")
    def test_recovery_refuses_symlink_and_hardlink_without_modifying_victim(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            path = device_log_paths(root, "testKey")[1]
            victim = root / "victim.txt"
            victim.write_bytes(b'{"ls":7')
            path.symlink_to(victim)
            with self.assertRaises(OSError):
                recover_incomplete_private_device_log_tail(path)
            path.unlink()
            os.link(victim, path)
            with self.assertRaises(OSError):
                recover_incomplete_private_device_log_tail(path)
            self.assertEqual(victim.read_bytes(), b'{"ls":7')

    def test_flask_retry_recovers_unterminated_tail_without_acknowledging_fragment(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            legacy, opaque = device_log_paths(directory, "legacySecret")
            legacy.write_bytes(b'{"ls":1}\n')
            opaque.write_bytes(b'{"ls":2}\n{"ls":3')
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "device_log_lock": device_log_lock,
                "append_private_device_log": append_private_device_log,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
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
            with app.test_client() as client:
                payload = '{"ls":3,"t":"RETRY"}\n'
                result = client.post("/log/legacySecret", data=payload)
                self.assertEqual((result.status_code, result.get_data(as_text=True)), (200, "OK n=1"))
                self.assertEqual(client.post("/log/legacySecret", data=payload).get_data(as_text=True), "OK n=0")
            self.assertEqual([json.loads(x)["ls"] for x in opaque.read_text().splitlines()], [2, 3])
            self.assertEqual(legacy.read_bytes(), b'{"ls":1}\n')

    def test_flask_recovery_fsync_failure_returns_500_and_retry_recovers(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            _, opaque = device_log_paths(directory, "legacySecret")
            opaque.write_bytes(b'{"ls":1}\n{"ls":2')
            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "device_log_lock": device_log_lock,
                "append_private_device_log": append_private_device_log,
                "read_device_log_lines": read_device_log_lines,
                "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
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
            with app.test_client() as client:
                with patch("device_log_storage.os.fsync",
                           side_effect=OSError("simulated recovery fsync failure")):
                    self.assertEqual(client.post("/log/legacySecret", data='{"ls":2}\n').status_code, 500)
                self.assertEqual(ns["_devlog_max_ls"], {})
                result = client.post("/log/legacySecret", data='{"ls":2}\n')
                self.assertEqual((result.status_code, result.get_data(as_text=True)), (200, "OK n=1"))
            self.assertEqual([json.loads(x)["ls"] for x in opaque.read_text().splitlines()], [1, 2])

if __name__ == "__main__":
    unittest.main()
