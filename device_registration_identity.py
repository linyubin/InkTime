"""Isolated v1 proof-of-possession primitive for future ESP32 registration.

NOT connected to Flask or deployed: ownership, counter persistence, and
firmware provisioning must be integrated and verified together before enabling.
"""
from __future__ import annotations

import hashlib
import hmac
import re

_DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z", re.ASCII)
_HEX_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_COUNTER = (1 << 63) - 1
_MAX_BODY = 2048


class RegistrationIdentityError(ValueError):
    """Invalid identity proof or a non-monotonic registration attempt."""


def validate_device_id(value: object) -> str:
    """Require a stable, bounded, canonical public identifier."""
    if not isinstance(value, str) or not _DEVICE_ID.fullmatch(value):
        raise RegistrationIdentityError("invalid device id")
    return value


def validate_registration_owner(expected_id: str, presented_id: str) -> None:
    """Fail closed: the endpoint belongs to an explicitly provisioned device."""
    expected = validate_device_id(expected_id)
    presented = validate_device_id(presented_id)
    if not hmac.compare_digest(expected.encode("ascii"), presented.encode("ascii")):
        raise RegistrationIdentityError("device is not the configured owner")


def _parse_counter(value: object) -> int:
    if (not isinstance(value, str) or not value.isascii() or
            not re.fullmatch(r"[1-9][0-9]{0,18}", value, re.ASCII)):
        raise RegistrationIdentityError("invalid registration counter")
    number = int(value)
    if number > _MAX_COUNTER:
        raise RegistrationIdentityError("registration counter out of range")
    return number


def registration_signing_payload(device_id: str, counter: str, body: bytes) -> bytes:
    """Canonical HMAC input; sign exact HTTP request bytes, not parsed JSON.

    Route and method are domain-separated constants, not untrusted headers.
    The legacy bearer URL key is intentionally not part of this payload.
    """
    device_id = validate_device_id(device_id)
    _parse_counter(counter)
    if not isinstance(body, bytes) or len(body) > _MAX_BODY:
        raise RegistrationIdentityError("invalid registration body")
    digest = hashlib.sha256(body).hexdigest()
    return (f"inktime-device-register-v1\nPOST\n/api/device_register\n"
            f"{device_id}\n{counter}\n{digest}\n").encode("ascii")


def verify_registration_proof(*, expected_id: str, device_id: str,
                              counter: str, signature: str, body: bytes,
                              credential: bytes, last_counter: int) -> int:
    """Verify v1 HMAC-SHA256 proof and strictly increasing persistent counter.

    The caller MUST serialize verification and durable last_counter publication
    under the endpoint lock before returning HTTP 200. No replay database,
    endpoint ownership or HTTP integration is provided by this pure function.
    """
    validate_registration_owner(expected_id, device_id)
    if not isinstance(credential, bytes) or len(credential) < 32:
        raise RegistrationIdentityError("invalid provisioned credential")
    if (type(last_counter) is not int or last_counter < 0 or
            last_counter > _MAX_COUNTER):
        raise RegistrationIdentityError("invalid stored counter")
    number = _parse_counter(counter)
    if (not isinstance(signature, str) or
            not _HEX_SIGNATURE.fullmatch(signature)):
        raise RegistrationIdentityError("invalid registration signature")
    payload = registration_signing_payload(device_id, counter, body)
    expected = hmac.new(credential, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise RegistrationIdentityError("invalid registration proof")
    if number <= last_counter:
        raise RegistrationIdentityError("replayed registration counter")
    return number
