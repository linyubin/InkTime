"""R1 admin authorization and browser Origin tests (no Flask dependency)."""
import base64
import unittest
from admin_security import admin_authorized, allowed_origin, is_device_protocol_path


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


if __name__ == "__main__":
    unittest.main()
