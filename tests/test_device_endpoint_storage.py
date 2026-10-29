"""R1: isolated endpoint.json atomicity, no-follow and fsync fault injection."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from device_endpoint_storage import load_device_endpoint, store_device_endpoint


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
