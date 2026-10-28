"""Opaque device-journal filenames with read-only legacy-key compatibility.

The filename digest is an identifier, NOT encryption or an authentication
mechanism. Rotate historically exposed/low-entropy bearer keys separately.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\\Z", re.ASCII)


def device_log_paths(directory: Path, key: str) -> tuple[Path, Path]:
    """Return (legacy plaintext-name path, new opaque-name path).

    Callers may READ both paths, but MUST write only to the second path.
    Invalid keys are rejected even when this helper is called independently
    of the HTTP authorization layer.
    """
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError("invalid device log key")
    root = Path(directory)
    digest = hashlib.sha256(key.encode("ascii")).hexdigest()
    return root / f"{key}.log", root / f"device-{digest}.log"
