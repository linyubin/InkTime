"""Pure helpers for protecting administrator routes and browser writes."""
import base64
import binascii
import hmac
import hashlib
import re
import secrets
from urllib.parse import urlsplit

# A signed double-submit token is bound to the current admin password.
# The cookie is host-only, Secure and SameSite=Strict at the Flask boundary.
CSRF_COOKIE_NAME = "__Host-inktime-csrf"
_CSRF_RE = re.compile(r"^[A-Za-z0-9_-]{43}\.[0-9a-f]{64}$")


def _csrf_signature(nonce: str, password: str) -> str:
    return hmac.new(
        password.encode("utf-8"), b"inktime:csrf:v1:" + nonce.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def make_csrf_token(password: str) -> str:
    """Create a per-browser session token; changing the admin password revokes it."""
    if not password:
        raise ValueError("Admin password must be configured")
    nonce = secrets.token_urlsafe(32)
    return nonce + "." + _csrf_signature(nonce, password)


def valid_csrf_token(cookie: str, submitted: str, password: str) -> bool:
    """Require a matching browser cookie and header/form value with a valid MAC."""
    if not password or not isinstance(cookie, str) or not isinstance(submitted, str):
        return False
    if not cookie or not submitted or not hmac.compare_digest(cookie, submitted):
        return False
    if not _CSRF_RE.fullmatch(cookie):
        return False
    nonce, signature = cookie.split(".", 1)
    return hmac.compare_digest(signature, _csrf_signature(nonce, password))


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
