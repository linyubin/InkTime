"""Isolated R1 registration owner/counter transaction tests (disposable POSIX only)."""
import hashlib
import hmac
import json
import os
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from device_endpoint_storage import _endpoint_lock, DeviceEndpointLockTimeout, load_device_endpoint, store_device_endpoint
from device_registration_identity import registration_signing_payload, RegistrationIdentityError
from device_registration_storage import (
    RegistrationStateError, provision_registration_owner,
    commit_verified_registration,
)

KEY = bytes(range(32))
BODY = b'{"port":8080,"alwayson":true}'


def _signature(counter, body=BODY, device_id="frame_01"):
    msg = registration_signing_payload(device_id, str(counter), body)
    return hmac.new(KEY, msg, hashlib.sha256).hexdigest()


def _hold_lock(path, acquired, release):
    with _endpoint_lock(Path(path)):
        acquired.set()
        if not release.wait(10):
            raise RuntimeError("lock release timeout")


class RegistrationTransactionTests(TestCase):
    def commit(self, path, counter, *, body=BODY, device_id="frame_01",
               signature=None, credential=KEY, peer_ip="192.168.1.7", **kwargs):
        return commit_verified_registration(
            path, expected_id="frame_01", device_id=device_id,
            counter=str(counter), signature=signature or _signature(counter, body, device_id),
            body=body, credential=credential, peer_ip=peer_ip, **kwargs)

    def test_offline_provision_then_signed_commit_preserves_reader_compatibility(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "device" / "endpoint.json"
            with self.assertRaises(RegistrationStateError):
                self.commit(path, 1)
            rec = provision_registration_owner(path, owner_id="frame_01")
            self.assertEqual(rec["_last_counter"], 0)
            self.assertEqual(load_device_endpoint(path), {})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            rec = self.commit(path, 1)
            self.assertEqual((rec["ip"], rec["port"], rec["_last_counter"]),
                             ("192.168.1.7", 8080, 1))
            self.assertTrue(load_device_endpoint(path)["alwayson"])
            self.assertEqual(self.commit(path, 2)["_last_counter"], 2)

    def test_replay_and_counter_rollback_do_not_modify_record(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            self.commit(path, 9)
            old = path.read_bytes()
            for counter in (9, 8, 1):
                with self.subTest(counter=counter), self.assertRaises(RegistrationIdentityError):
                    self.commit(path, counter)
                self.assertEqual(path.read_bytes(), old)

    def test_wrong_identity_signature_or_modified_body_does_not_write(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            old = path.read_bytes()
            for changes in ({"device_id": "frame_02"},
                            {"credential": b"x" * 32},
                            {"signature": "0" * 64},
                            {"body": b'{"port":8080, "alwayson":true}',
                             "signature": _signature(1)}):
                with self.subTest(changes=changes), self.assertRaises(RegistrationIdentityError):
                    self.commit(path, 1, **changes)
                self.assertEqual(path.read_bytes(), old)

    def test_existing_legacy_file_cannot_be_silently_claimed(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            store_device_endpoint(path, {"ip": "10.0.0.2", "port": 80})
            old = path.read_bytes()
            with self.assertRaises(RegistrationStateError):
                provision_registration_owner(path, owner_id="frame_01")
            with self.assertRaises(RegistrationStateError):
                self.commit(path, 1)
            self.assertEqual(path.read_bytes(), old)

    def test_missing_state_after_registration_cannot_reset_counter(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            self.commit(path, 7)
            path.unlink()  # Simulate loss, never auto-enroll from a signed request.
            with self.assertRaises(RegistrationStateError):
                self.commit(path, 8)
            self.assertFalse(path.exists())

    def test_corrupt_or_duplicate_stored_state_fails_closed(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            good = json.loads(path.read_text())
            samples = (b"{invalid", b"x" * 4097,
                       json.dumps({**good, "_last_counter": "0"}).encode(),
                       json.dumps({**good, "_device_id": "frame_02"}).encode(),
                       path.read_bytes().replace(b'"_last_counter":0',
                                                 b'"_last_counter":0,"_last_counter":0'))
            for data in samples:
                with self.subTest(data=data[:45]):
                    path.write_bytes(data)
                    with self.assertRaises((RegistrationStateError, OSError)):
                        self.commit(path, 1)
                    self.assertEqual(path.read_bytes(), data)

    def test_signed_body_must_have_unambiguous_port_and_boolean(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            old = path.read_bytes()
            for body in (b'{"port":80,"port":81,"alwayson":true}',
                         b'{"port":80,"alwayson":1}',
                         b'{"port":80,"alwayson":true,"ip":"10.0.0.2"}',
                         b'{"port":80}', b'{"port":0,"alwayson":true}',
                         b'{"port":80,"alwayson":true}\xff'):
                with self.subTest(body=body), self.assertRaises((RegistrationStateError, ValueError)):
                    self.commit(path, 1, body=body)
                self.assertEqual(path.read_bytes(), old)

    def test_pre_rename_fsync_error_keeps_counter_and_endpoint(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            self.commit(path, 1)
            old = path.read_bytes()
            with patch("device_registration_storage.os.fsync", side_effect=OSError("disk fault")):
                with self.assertRaises(OSError):
                    self.commit(path, 2)
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(list(Path(d).glob("*.tmp")), [])

    def test_post_rename_fsync_error_requires_new_counter_for_retry(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            real_fsync = os.fsync
            calls = 0
            def fail_dir(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync fault")
                return real_fsync(fd)
            with patch("device_registration_storage.os.fsync", side_effect=fail_dir):
                with self.assertRaises(OSError):
                    self.commit(path, 1)
            self.assertEqual(calls, 2)
            self.assertEqual(json.loads(path.read_text())["_last_counter"], 1)
            with self.assertRaises(RegistrationIdentityError):
                self.commit(path, 1)
            self.assertEqual(self.commit(path, 2)["_last_counter"], 2)

    def test_cross_process_lock_timeout_and_no_ack(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "endpoint.json"
            provision_registration_owner(path, owner_id="frame_01")
            ctx = get_context("spawn")
            acquired, release = ctx.Event(), ctx.Event()
            proc = ctx.Process(target=_hold_lock, args=(str(path), acquired, release))
            proc.start()
            try:
                self.assertTrue(acquired.wait(8))
                with self.assertRaises(DeviceEndpointLockTimeout):
                    self.commit(path, 1, timeout_seconds=0.1)
                self.assertEqual(json.loads(path.read_text())["_last_counter"], 0)
            finally:
                release.set()
                proc.join(8)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(5)
            self.assertEqual(proc.exitcode, 0)
            self.assertEqual(self.commit(path, 1)["_last_counter"], 1)

    def test_symlink_and_hardlink_state_never_written(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            victim = root / "victim"
            victim.write_text("unchanged")
            path = root / "endpoint.json"
            path.symlink_to(victim)
            with self.assertRaises(OSError):
                provision_registration_owner(path, owner_id="frame_01")
            path.unlink()
            os.link(victim, path)
            with self.assertRaises((RegistrationStateError, OSError)):
                self.commit(path, 1)
            self.assertEqual(victim.read_text(), "unchanged")


if __name__ == "__main__":
    import unittest
    unittest.main()
