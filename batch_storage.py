"""Immutable, validated InkTime image batches with atomic current-pointer publication.

This module is intentionally independent of the legacy render pipeline: callers
must create an isolated, complete source directory before publish_batch().
A future integration PR will make both cron and WebUI render into such directories.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid


PANEL_SHAPES = {"portrait": (480, 800), "landscape": (800, 480)}
_BATCH_ID = re.compile(r"^[0-9a-f]{32}$")


class BatchValidationError(ValueError):
    """The requested batch contains incomplete or inconsistent artifacts."""


def validate_photo(bin_path: Path, sidecar_path: Path) -> dict:
    """Validate a binary+sidecar pair without changing either original file."""
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchValidationError(f"Invalid photo sidecar: {sidecar_path.name}") from exc

    if not isinstance(sidecar, dict):
        raise BatchValidationError("Sidecar must be a JSON object")
    orientation = sidecar.get("orientation")
    if orientation not in PANEL_SHAPES:
        raise BatchValidationError("Unsupported photo orientation")

    expected_width, expected_height = PANEL_SHAPES[orientation]
    width, height = sidecar.get("w"), sidecar.get("h")
    if (type(width) is not int or type(height) is not int
            or (width, height) != (expected_width, expected_height)):
        raise BatchValidationError("Sidecar dimensions do not match panel orientation")

    try:
        payload = bin_path.read_bytes()
    except OSError as exc:
        raise BatchValidationError(f"Missing photo binary: {bin_path.name}") from exc
    if len(payload) != width * height:
        raise BatchValidationError(f"Unexpected BIN size: {len(payload)}, expected {width * height}")
    if payload and max(payload) > 3:
        raise BatchValidationError("Unknown palette index in photo BIN")
    return {
        "orientation": orientation,
        "width": width,
        "height": height,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def publish_batch(storage_root: Path, isolated_source: Path, indices: list[int]) -> dict:
    """Validate, stage, and atomically publish a complete, immutable batch.

    Source must be an isolated directory not being written by another process.
    This method does not protect mutable legacy output directories from races.
    """
    if not indices or len(indices) != len(set(indices)) or any(type(i) is not int or i < 0 for i in indices):
        raise BatchValidationError("Expected non-empty, unique, nonnegative photo indices")
    root = Path(storage_root)
    source = Path(isolated_source)
    batch_id = uuid.uuid4().hex
    staging_root = root / ".staging"
    staging_dir = staging_root / batch_id
    published_dir = root / "batches" / batch_id
    staging_root.mkdir(parents=True, exist_ok=True)
    published_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir()

    photos = []
    try:
        for idx in sorted(indices):
            binary = source / f"photo_{idx}.bin"
            sidecar = source / f"photo_{idx}.json"
            info = validate_photo(binary, sidecar)
            shutil.copyfile(binary, staging_dir / binary.name)
            shutil.copyfile(sidecar, staging_dir / sidecar.name)
            # Verify again after copy, so the manifest hashes what will be published.
            validated = validate_photo(staging_dir / binary.name, staging_dir / sidecar.name)
            if info != validated:
                raise BatchValidationError("Source photo changed during snapshot")
            photos.append({"index": idx, **validated})

        manifest = {
            "protocol": 1,
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "photos": photos,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        # Atomic directory rename: readers can only see a complete published batch.
        os.replace(staging_dir, published_dir)

        # Atomic pointer rename: failed preparation leaves the previous batch active.
        current = root / "current.json"
        pointer_temp = root / f".current-{batch_id}.tmp"
        try:
            pointer_temp.write_text(
                json.dumps({"protocol": 1, "batch_id": batch_id}), encoding="utf-8"
            )
            os.replace(pointer_temp, current)
        finally:
            pointer_temp.unlink(missing_ok=True)
        return manifest
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


def read_current(storage_root: Path) -> dict:
    """Read the active version's manifest without following user-provided paths."""
    root = Path(storage_root)
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    batch_id = pointer.get("batch_id")
    if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id):
        raise BatchValidationError("Invalid current batch pointer")
    manifest = json.loads((root / "batches" / batch_id / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("batch_id") != batch_id or manifest.get("protocol") != 1:
        raise BatchValidationError("Current pointer disagrees with manifest")
    return manifest
