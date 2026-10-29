"""Atomic, fail-closed POSIX storage for the registered ESP32 LAN endpoint.

Shares the journal's no-follow directory traversal; not a device-authentication
mechanism or evidence of NAS durability.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat

from device_log_storage import _open_journal_directory, _assert_journal_directory_identity
from device_endpoint_security import validate_device_endpoint

_MAX_BYTES = 4096


def _check_target(directory_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("unsafe device endpoint target")


def load_device_endpoint(path: Path) -> dict:
    """Never follow symlinks; an invalid record means the device is offline."""
    path = Path(path)
    if path.name != "endpoint.json":
        raise ValueError("invalid endpoint filename")
    try:
        directory_fd = _open_journal_directory(path.parent)
    except OSError:
        return {}
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         dir_fd=directory_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_BYTES:
                    raise OSError("unsafe endpoint file")
                with os.fdopen(os.dup(fd), "rb") as stream:
                    data = stream.read(_MAX_BYTES + 1)
                after = os.fstat(fd)
                if (len(data) > _MAX_BYTES or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise OSError("endpoint changed during read")
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError("endpoint replaced during read")
            finally:
                os.close(fd)
            _assert_journal_directory_identity(path.parent, directory_fd)
            rec = json.loads(data.decode("utf-8"))
            if not isinstance(rec, dict):
                return {}
            ip, port = validate_device_endpoint(rec.get("ip"), rec.get("port", 80))
            return {**rec, "ip": ip, "port": port}
        except (OSError, ValueError, TypeError, UnicodeError):
            return {}
    finally:
        os.close(directory_fd)


def store_device_endpoint(path: Path, rec: dict) -> None:
    """Publish a 0600 temporary file via rename, then fsync the directory.

    An error after rename may leave the new value visible without an HTTP ACK;
    callers must return an error and allow the device to retry registration.
    """
    path = Path(path)
    if path.name != "endpoint.json" or not isinstance(rec, dict):
        raise ValueError("invalid endpoint record/path")
    ip, port = validate_device_endpoint(rec.get("ip"), rec.get("port", 80))
    payload = json.dumps({**rec, "ip": ip, "port": port}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_BYTES:
        raise ValueError("endpoint record too large")
    directory_fd = _open_journal_directory(path.parent, create=True)
    temp_name = None
    try:
        _check_target(directory_fd, path.name)
        temp_name = f".endpoint.json.{secrets.token_hex(16)}.tmp"
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                count = os.write(fd, payload[offset:])
                if count <= 0:
                    raise OSError("short endpoint write")
                offset += count
            os.fsync(fd)
        finally:
            os.close(fd)
        _assert_journal_directory_identity(path.parent, directory_fd)
        _check_target(directory_fd, path.name)
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_name = None
        os.fsync(directory_fd)
        _assert_journal_directory_identity(path.parent, directory_fd)
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
