"""Pure helpers for protecting administrator routes and browser writes."""
import base64
import binascii
import hmac
from urllib.parse import urlsplit


_DEVICE_PREFIXES = ("/static/inktime/", "/api/device_log/", "/api/device_register/")


def is_device_protocol_path(path: str) -> bool:
    return path.startswith(_DEVICE_PREFIXES)


def admin_authorized(authorization: str, username: str, password: str) -> bool:
    if not password or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True)
        user, secret = decoded.decode("utf-8").split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    return hmac.compare_digest(user, username) and hmac.compare_digest(secret, password)


def allowed_origin(origin: str, host: str) -> bool:
    """A browser Origin, when present, must match the destination HTTP Host."""
    if not origin:
        return True  # CLI clients still require administrator authentication.
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.lower()
    except ValueError:
        return False
