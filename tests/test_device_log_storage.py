"""R1/PR-02: device journal filenames never persist plaintext bearer keys."""
import ast
import copy
from datetime import datetime
import json
import math
import multiprocessing
import time
import os
from pathlib import Path
import re
import stat
from unittest.mock import patch
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from flask import Flask, abort, request

from device_log_storage import (device_log_paths, device_log_lock, DeviceJournalLockTimeout,
                                append_private_device_log,
                                read_device_log_lines, recover_incomplete_private_device_log_tail,
                                sync_existing_private_device_log)
from download_key_security import authorized_download_key, resolve_download_keys

ROOT = Path(__file__).resolve().parents[1]


def _hold_lock_in_child(path, entered, release):
    with device_log_lock(path):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("lock test timed out")




def extract_function(filename, name, namespace):
    path = ROOT / filename
    namespace.setdefault("DeviceJournalLockTimeout", DeviceJournalLockTimeout)
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

    def test_lock_timeout_configuration_rejects_invalid_values(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            for value in (-1, 61, math.nan, math.inf, -math.inf, True, "5", None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    with device_log_lock(path, timeout_seconds=value):
                        self.fail("invalid timeout acquired lock")
            self.assertEqual(list(Path(d).iterdir()), [])

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods()
                         and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX fork and flock")
    def test_lock_contention_times_out_and_later_recovers(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "testKey")[1]
            ctx = multiprocessing.get_context("fork")
            entered, release = ctx.Event(), ctx.Event()
            holder = ctx.Process(target=_hold_lock_in_child, args=(path, entered, release))
            try:
                holder.start()
                self.assertTrue(entered.wait(5))
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    with device_log_lock(path, timeout_seconds=0.15):
                        self.fail("contended lock acquired")
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertFalse(path.exists())  # no upload without a lock
                self.assertTrue(path.with_name(path.name + ".lock").exists())
            finally:
                release.set()
                if holder.pid is not None:
                    holder.join(5)
                    if holder.is_alive():
                        holder.terminate()
                        holder.join(5)
            self.assertEqual(holder.exitcode, 0)
            with device_log_lock(path, timeout_seconds=0):
                pass  # released lock can be acquired immediately

    def test_flask_upload_lock_timeout_returns_retryable_503_without_ack(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            _, opaque = device_log_paths(directory, "legacySecret")

            def busy_lock(_path):
                raise DeviceJournalLockTimeout("synthetic contention")

            ns = {
                "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024,
                "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
                "device_log_lock": busy_lock,
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
                response = client.post("/log/legacySecret", data='{"ls":1}\n')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers.get("Retry-After"), "1")
            # An unrelated timeout is an I/O failure, not lock contention.
            def unrelated_timeout(_path):
                raise TimeoutError("synthetic filesystem timeout")
            ns["device_log_lock"] = unrelated_timeout
            with app.test_client() as client:
                other = client.post("/log/legacySecret", data='{"ls":1}\n')
            self.assertEqual(other.status_code, 500)
            self.assertIsNone(other.headers.get("Retry-After"))
            self.assertFalse(opaque.exists())
            self.assertEqual(ns["_devlog_max_ls"], {})

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


    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_ancestor_symlink_rejected_for_all_journal_operations(self):
        with TemporaryDirectory() as d:
            real = Path(d) / "real"
            (real / "device").mkdir(parents=True)
            alias = Path(d) / "alias"
            alias.symlink_to(real, target_is_directory=True)
            path = device_log_paths(alias / "device", "testKey")[1]
            victim = real / "device" / path.name
            original = b'{"ls":1}\n'
            victim.write_bytes(original)
            actions = {
                "read": lambda: read_device_log_lines(path),
                "sync": lambda: sync_existing_private_device_log(path),
                "recover": lambda: recover_incomplete_private_device_log_tail(path),
                "append": lambda: append_private_device_log(
                    path, ['{"ls":2}'], max_bytes=100),
                "lock": lambda: self._attempt_journal_lock(path),
            }
            for name, action in actions.items():
                with self.subTest(operation=name), self.assertRaises(OSError):
                    action()
            self.assertEqual(victim.read_bytes(), original)
            self.assertFalse((real / "device" / (path.name + ".lock")).exists())

    @staticmethod
    def _attempt_journal_lock(path):
        with device_log_lock(path):
            pass

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
                         "requires POSIX no-follow directory-relative opens")
    def test_nested_real_directories_work_and_dotdot_fails_closed(self):
        with TemporaryDirectory() as d:
            directory = Path(d) / "a" / "b" / "device"
            path = device_log_paths(directory, "testKey")[1]
            with device_log_lock(path):
                append_private_device_log(path, ['{"ls":1}'], max_bytes=100)
                self.assertEqual(read_device_log_lines(path, validate_jsonl=True),
                                 ['{"ls":1}'])
                sync_existing_private_device_log(path)
                self.assertEqual(recover_incomplete_private_device_log_tail(path), 0)
            self.assertEqual(path.read_text(), '{"ls":1}\n')
            unsafe = Path(d) / "a" / ".." / "a" / "b" / "device" / path.name
            for action in (lambda: read_device_log_lines(unsafe),
                           lambda: append_private_device_log(
                               unsafe, ['{"ls":2}'], max_bytes=100),
                           lambda: self._attempt_journal_lock(unsafe)):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual(path.read_text(), '{"ls":1}\n')

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


class DeviceLogIntegrityTests(unittest.TestCase):
    """R1: malformed acknowledged JSONL must never silently reset the cursor."""

    def _app(self, directory):
        ns = {
            "DEVICE_LOG_DIR": directory, "DEVICE_LOG_MAX_BYTES": 1024 * 1024,
            "_devlog_max_ls": {}, "device_log_paths": device_log_paths,
            "device_log_lock": device_log_lock,
            "append_private_device_log": append_private_device_log,
            "read_device_log_lines": read_device_log_lines,
            "recover_incomplete_private_device_log_tail": recover_incomplete_private_device_log_tail,
            "DOWNLOAD_KEYS": ("legacySecret",),
            "authorized_download_key": authorized_download_key,
            "json": json, "datetime": datetime, "request": request,
            "abort": abort, "Path": Path,
        }
        extract_function("server.py", "_devlog_path", ns)
        extract_function("server.py", "_devlog_load_max_ls", ns)
        endpoint = extract_function("server.py", "esp_device_log", ns)
        app = Flask(__name__)

        @app.post("/log/<key>")
        def upload(key):
            return endpoint(key)

        return app, ns

    def test_strict_reader_rejects_corrupt_complete_records_without_writes(self):
        with TemporaryDirectory() as d:
            path = device_log_paths(Path(d), "legacySecret")[1]
            valid = json.dumps({"ls": 1, "msg": "a\u2028b"}, ensure_ascii=False) + "\n"
            path.write_bytes(valid.encode("utf-8"))
            self.assertEqual(read_device_log_lines(path, validate_jsonl=True),
                             [valid[:-1]])
            invalid = (
                b'{"ls":1}',                 # unterminated final line
                b'{"ls":1}\nnot-json\n',
                b'{"ls":true}\n',
                b'{"ls":"2"}\n',
                b'{"ls":1,"v":NaN}\n',
                b'{"ls":1}\n\n',
                b'{"ls":1}\n\xff\n',
                b'[]\n',
            )
            for payload in invalid:
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(OSError):
                        read_device_log_lines(path, validate_jsonl=True)
                    self.assertEqual(path.read_bytes(), payload)

    def test_flask_rejects_malformed_complete_record_in_all_generations(self):
        for generation in ("legacy", "legacy_rotated", "opaque", "opaque_rotated"):
            with self.subTest(generation=generation), TemporaryDirectory() as d:
                directory = Path(d)
                legacy, opaque = device_log_paths(directory, "legacySecret")
                targets = {
                    "legacy": legacy,
                    "legacy_rotated": legacy.with_suffix(".log.1"),
                    "opaque": opaque,
                    "opaque_rotated": opaque.with_suffix(".log.1"),
                }
                target = targets[generation]
                payload = b'{"ls":2}\n{"ls":bad}\n'
                target.write_bytes(payload)
                app, ns = self._app(directory)
                with app.test_client() as client:
                    result = client.post("/log/legacySecret", data='{"ls":3}\n')
                self.assertEqual(result.status_code, 500)
                self.assertEqual(ns["_devlog_max_ls"], {})
                self.assertEqual(target.read_bytes(), payload)
                if target != opaque:
                    self.assertFalse(opaque.exists())

    def test_rotated_unterminated_record_fails_closed_without_truncation(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            _, opaque = device_log_paths(directory, "legacySecret")
            rotated = opaque.with_suffix(".log.1")
            payload = b'{"ls":7}\n{"ls":8'
            rotated.write_bytes(payload)
            app, ns = self._app(directory)
            with app.test_client() as client:
                result = client.post("/log/legacySecret", data='{"ls":8}\n')
            self.assertEqual(result.status_code, 500)
            self.assertEqual(rotated.read_bytes(), payload)
            self.assertFalse(opaque.exists())
            self.assertEqual(ns["_devlog_max_ls"], {})

    def test_cursor_uses_all_valid_records_not_only_last_500(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            legacy, opaque = device_log_paths(directory, "legacySecret")
            legacy.write_text("".join('{"ls":%d}\n' % i for i in
                                      range(601, 0, -1)), encoding="utf-8")
            app, _ = self._app(directory)
            with app.test_client() as client:
                self.assertEqual(
                    client.post("/log/legacySecret", data='{"ls":600}\n').get_data(as_text=True),
                    "OK n=0")
                self.assertEqual(
                    client.post("/log/legacySecret", data='{"ls":602}\n').get_data(as_text=True),
                    "OK n=1")
            self.assertEqual([json.loads(x)["ls"] for x in
                              opaque.read_text(encoding="utf-8").splitlines()], [602])


if __name__ == "__main__":
    unittest.main()
