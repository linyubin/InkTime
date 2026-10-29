"""R1: isolated endpoint.json atomicity, no-follow and fsync fault injection."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from multiprocessing import get_context
from unittest import TestCase
from unittest.mock import patch

from device_endpoint_storage import (load_device_endpoint, store_device_endpoint,
                                     DeviceEndpointLockTimeout, _endpoint_lock)


def _hold_endpoint_lock(path_text, acquired, release):
    with _endpoint_lock(Path(path_text)):
        acquired.set()
        if not release.wait(10):
            raise RuntimeError("test lock release timed out")


class EndpointStorageTests(TestCase):
    def test_atomic_roundtrip_and_private_permissions(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "device" / "endpoint.json"
            self.assertEqual(load_device_endpoint(path), {})
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": "8080"})
            self.assertEqual(load_device_endpoint(path)["port"], 8080)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            store_device_endpoint(path, {"ip": "10.0.0.2", "port": 80})
            self.assertEqual(load_device_endpoint(path)["ip"], "10.0.0.2")

    def test_symlink_and_hardlink_targets_fail_closed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            folder = root / "device"
            folder.mkdir()
            victim = root / "victim"
            victim.write_text("untouched")
            path = folder / "endpoint.json"
            path.symlink_to(victim)
            self.assertEqual(load_device_endpoint(path), {})
            with self.assertRaises(OSError):
                store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            path.unlink()
            os.link(victim, path)
            self.assertEqual(load_device_endpoint(path), {})
            with self.assertRaises(OSError):
                store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(victim.read_text(), "untouched")

    def test_symlink_ancestor_fails_closed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "actual").mkdir()
            (root / "alias").symlink_to(root / "actual", target_is_directory=True)
            path = root / "alias" / "endpoint.json"
            self.assertEqual(load_device_endpoint(path), {})
            with self.assertRaises(OSError):
                store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertFalse((root / "actual" / "endpoint.json").exists())

    def test_pre_rename_fsync_failure_preserves_previous(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80})
            old = path.read_bytes()
            with patch("device_endpoint_storage.os.fsync", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(list(Path(d).glob("*.tmp")), [])

    def test_post_rename_directory_fsync_failure_does_not_ack(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80})
            real_fsync = os.fsync
            calls = 0
            def fail_directory(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")
                return real_fsync(fd)
            with patch("device_endpoint_storage.os.fsync", side_effect=fail_directory):
                with self.assertRaises(OSError):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(calls, 2)
            self.assertEqual(load_device_endpoint(path)["ip"], "10.1.2.3")

    def test_corrupt_oversize_and_non_lan_records_fail_closed(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            for data in (b"{bad json", b"x" * 4097,
                         json.dumps({"ip": "8.8.8.8", "port": 80}).encode()):
                path.write_bytes(data)
                self.assertEqual(load_device_endpoint(path), {})
            with self.assertRaises(ValueError):
                store_device_endpoint(path, {"ip": "8.8.8.8", "port": 80})

    def test_short_write_loop_and_zero_write_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            real_write = os.write
            def partial(fd, buf):
                return real_write(fd, buf[:3])
            with patch("device_endpoint_storage.os.write", side_effect=partial):
                store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80})
            self.assertEqual(load_device_endpoint(path)["ip"], "192.168.1.8")
            old = path.read_bytes()
            with patch("device_endpoint_storage.os.write", return_value=0):
                with self.assertRaises(OSError):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(path.read_bytes(), old)

    def test_cross_process_lock_timeout_does_not_replace_record(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80})
            ctx = get_context("spawn")
            acquired, release = ctx.Event(), ctx.Event()
            proc = ctx.Process(target=_hold_endpoint_lock, args=(str(path), acquired, release))
            proc.start()
            try:
                self.assertTrue(acquired.wait(8), "child did not acquire lock")
                with self.assertRaises(DeviceEndpointLockTimeout):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80},
                                          timeout_seconds=0.1)
                self.assertEqual(load_device_endpoint(path)["ip"], "192.168.1.8")
            finally:
                release.set()
                proc.join(8)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(5)
            self.assertEqual(proc.exitcode, 0)
            store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(load_device_endpoint(path)["ip"], "10.1.2.3")

    def test_manual_merge_preserves_latest_alwayson(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80,
                                         "alwayson": True})
            updated = store_device_endpoint(path, {"ip": "10.1.2.3", "port": 8080,
                                                   "manual": True}, preserve_alwayson=True)
            self.assertTrue(updated["alwayson"])
            self.assertTrue(load_device_endpoint(path)["alwayson"])

    def test_unsafe_endpoint_lock_targets_fail_closed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            path, lock, victim = root / "endpoint.json", root / ".endpoint.json.lock", root / "victim"
            victim.write_text("untouched")
            lock.symlink_to(victim)
            with self.assertRaises(OSError):
                store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            lock.unlink()
            os.link(victim, lock)
            with self.assertRaises(OSError):
                store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(victim.read_text(), "untouched")
            self.assertFalse(path.exists())

    def test_lock_inode_replacement_rejects_ack(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            path = root / "endpoint.json"
            store_device_endpoint(path, {"ip": "192.168.1.8", "port": 80})
            real_replace = os.replace
            def swap_lock(src, dst, **kwargs):
                result = real_replace(src, dst, **kwargs)
                lock = root / ".endpoint.json.lock"
                lock.rename(root / "old.lock")
                lock.write_bytes(b"replacement")
                return result
            with patch("device_endpoint_storage.os.replace", side_effect=swap_lock):
                with self.assertRaises(OSError):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80})
            self.assertEqual(load_device_endpoint(path)["ip"], "10.1.2.3")

    def test_invalid_lock_timeouts(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            for value in (-1, 61, True, float("nan"), "5"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store_device_endpoint(path, {"ip": "10.1.2.3", "port": 80},
                                          timeout_seconds=value)
