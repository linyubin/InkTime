"""R1 admin authorization and browser Origin tests (no Flask dependency)."""
import base64
import unittest
from admin_security import (admin_authorized, allowed_origin, is_device_protocol_path,
                            make_csrf_token, valid_csrf_token)


def auth(value: str) -> str:
    return "Basic " + base64.b64encode(value.encode()).decode()


class AdminSecurityTests(unittest.TestCase):
    def test_admin_credentials(self):
        self.assertTrue(admin_authorized(auth("admin:secret:colon"), "admin", "secret:colon"))
        self.assertFalse(admin_authorized(auth("admin:wrong"), "admin", "secret"))
        self.assertFalse(admin_authorized(auth("other:secret"), "admin", "secret"))
        self.assertFalse(admin_authorized("Basic not_base64!", "admin", "secret"))
        self.assertFalse(admin_authorized(auth("admin:secret"), "admin", ""))

    def test_device_route_allowlist(self):
        for path in ("/static/inktime/key/photo_0.bin", "/api/device_log/key",
                     "/api/device_register/key"):
            self.assertTrue(is_device_protocol_path(path))
        for path in ("/api/device/config", "/images/a.jpg", "/static/style_editor/a.jpg"):
            self.assertFalse(is_device_protocol_path(path))

    def test_cross_origin(self):
        self.assertTrue(allowed_origin("https://example.org:8765", "example.org:8765"))
        self.assertTrue(allowed_origin("", "example.org"))
        self.assertFalse(allowed_origin("https://evil.org", "example.org"))
        self.assertFalse(allowed_origin("https://example.org.evil.org", "example.org"))
        self.assertFalse(allowed_origin("null", "example.org"))

    def test_csrf_round_trip_and_randomness(self):
        first = make_csrf_token("admin-password")
        second = make_csrf_token("admin-password")
        self.assertNotEqual(first, second)
        self.assertTrue(valid_csrf_token(first, first, "admin-password"))
        self.assertTrue(valid_csrf_token(second, second, "admin-password"))

    def test_csrf_rejects_missing_and_mismatched_tokens(self):
        token = make_csrf_token("admin-password")
        for cookie, submitted in (("", token), (token, ""), (token, "other"),
                                  (None, token), (token, None)):
            with self.subTest(cookie=cookie, submitted=submitted):
                self.assertFalse(valid_csrf_token(cookie, submitted, "admin-password"))

    def test_csrf_rejects_tampering_and_password_rotation(self):
        token = make_csrf_token("admin-password")
        self.assertFalse(valid_csrf_token(token, token, "rotated-password"))
        altered = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertFalse(valid_csrf_token(altered, altered, "admin-password"))
        self.assertFalse(valid_csrf_token(token, token, ""))

    def test_csrf_rejects_malformed_or_oversized_cookie(self):
        for cookie in ("bad", "a" * 2000, "a" * 43 + "." + "x" * 64,
                       "a" * 43 + "." + "0" * 64, "a" * 43 + "." + "0" * 64 + ";x"):
            with self.subTest(cookie=cookie[:20]):
                self.assertFalse(valid_csrf_token(cookie, cookie, "admin-password"))

    def test_csrf_requires_configured_password(self):
        with self.assertRaises(ValueError):
            make_csrf_token("")


if __name__ == "__main__":
    unittest.main()
