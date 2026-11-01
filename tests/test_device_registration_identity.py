"""Isolated protocol-contract tests: no Flask, NAS, network or ESP32 involved."""
import hashlib
import hmac
import unittest

from device_registration_identity import (
    RegistrationIdentityError,
    registration_signing_payload,
    validate_device_id,
    validate_registration_owner,
    verify_registration_proof,
)

KEY = bytes(range(32))
BODY = b'{"port":80,"alwayson":true}'


def proof(device_id="frame_01", counter="1", body=BODY, key=KEY):
    message = registration_signing_payload(device_id, counter, body)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class DeviceRegistrationIdentityTests(unittest.TestCase):
    def verify(self, **overrides):
        args = dict(expected_id="frame_01", device_id="frame_01", counter="1",
                    signature=proof(), body=BODY, credential=KEY, last_counter=0)
        args.update(overrides)
        return verify_registration_proof(**args)

    def test_accepts_valid_signature_and_increasing_counter(self):
        self.assertEqual(self.verify(), 1)
        self.assertEqual(self.verify(counter="12", signature=proof(counter="12"),
                                     last_counter=11), 12)

    def test_rejects_body_tampering_including_json_whitespace(self):
        for changed in (b'{"port":81,"alwayson":true}',
                        b'{"port":80, "alwayson":true}', b''):
            with self.subTest(body=changed), self.assertRaises(RegistrationIdentityError):
                self.verify(body=changed)

    def test_rejects_wrong_key_and_signature(self):
        with self.assertRaises(RegistrationIdentityError):
            self.verify(credential=b"x" * 32)
        with self.assertRaises(RegistrationIdentityError):
            self.verify(signature="0" * 64)

    def test_rejects_wrong_owner_even_with_valid_proof(self):
        with self.assertRaises(RegistrationIdentityError):
            self.verify(device_id="frame_02", signature=proof(device_id="frame_02"))
        with self.assertRaises(RegistrationIdentityError):
            validate_registration_owner("frame_01", "frame_02")

    def test_rejects_replay_and_counter_rollback(self):
        for previous in (1, 2, 99):
            with self.subTest(previous=previous), self.assertRaises(RegistrationIdentityError):
                self.verify(last_counter=previous)

    def test_rejects_noncanonical_ids(self):
        for value in ("", "Frame_01", "../frame", "frame/01", "frame.01",
                      " frame_01", "frame_01 ", "a" * 33, "设备", None, 1):
            with self.subTest(value=value), self.assertRaises(RegistrationIdentityError):
                validate_device_id(value)

    def test_rejects_noncanonical_counters(self):
        for value in ("", "0", "01", "+1", "-1", " 1", "1 ", "１",
                      "9223372036854775808", "1" * 21, 1, True, None):
            with self.subTest(value=value), self.assertRaises(RegistrationIdentityError):
                registration_signing_payload("frame_01", value, BODY)

    def test_rejects_bad_signature_and_key_types(self):
        for value in ("F" * 64, "0" * 63, "0" * 65, "", None, b"0" * 64):
            with self.subTest(value=value), self.assertRaises(RegistrationIdentityError):
                self.verify(signature=value)
        for key in (b"x" * 31, "x" * 32, None):
            with self.subTest(key=key), self.assertRaises(RegistrationIdentityError):
                self.verify(credential=key)

    def test_rejects_bad_stored_counter(self):
        for value in (-1, True, 1.0, "0", None, 2**63):
            with self.subTest(value=value), self.assertRaises(RegistrationIdentityError):
                self.verify(last_counter=value)

    def test_rejects_oversized_or_nonbytes_body(self):
        for body in (b"x" * 2049, '{"port":80}', bytearray(BODY), None):
            with self.subTest(type=type(body)), self.assertRaises(RegistrationIdentityError):
                self.verify(body=body)

    def test_payload_is_domain_separated_and_deterministic(self):
        p = registration_signing_payload("frame_01", "1", BODY)
        self.assertTrue(p.startswith(b"inktime-device-register-v1\nPOST\n/api/device_register\n"))
        self.assertIn(hashlib.sha256(BODY).hexdigest().encode("ascii"), p)
        self.assertEqual(p, registration_signing_payload("frame_01", "1", BODY))
        self.assertNotIn(BODY, p)


if __name__ == "__main__":
    unittest.main()
