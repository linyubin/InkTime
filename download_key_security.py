"""Pure helpers for staged ESP32 download-key rotation (R1/PR-02).

A previous key is accepted only when explicitly supplied by the operator.
"""
from __future__ import annotations
import hmac
import re
from typing import Mapping

_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)

def _validate_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise ValueError(f"{label} must be a nonempty URL-safe path segment (max 128 characters)")
    return value

def resolve_download_keys(config_key: object, environ: Mapping[str, str]) -> tuple[str, ...]:
    """Explicit empty env override fails closed; previous key is opt-in only."""
    current = environ["INKTIME_DOWNLOAD_KEY"] if "INKTIME_DOWNLOAD_KEY" in environ else config_key
    active = _validate_key(current, "active download key")
    previous = environ.get("INKTIME_DOWNLOAD_KEY_PREVIOUS", "")
    if not previous:
        return (active,)
    previous = _validate_key(previous, "previous download key")
    if hmac.compare_digest(active, previous):
        raise ValueError("previous download key must differ from active key")
    return (active, previous)

def authorized_download_key(presented: object, keys: tuple[str, ...]) -> bool:
    """Authorize exact match against active and explicitly enabled previous key."""
    if not isinstance(presented, str) or not presented or not keys:
        return False
    matched = False
    for candidate in keys:
        matched |= hmac.compare_digest(presented, candidate)
    return matched
