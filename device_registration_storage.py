"""Staging-only, single-owner registration state transaction.

NOT wired to Flask or firmware. A provisioned endpoint.json owns the monotonic
counter and endpoint in one atomic record. Never auto-enroll or reset a missing
counter from a network registration. POSIX locking/fsync are not NAS evidence.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime
from pathlib import Path
import secrets
import stat

from device_endpoint_security import validate_device_endpoint
from device_endpoint_storage import _MAX_BYTES, _check_target, _endpoint_lock
from device_log_storage import _assert_journal_directory_identity
from device_registration_identity import (
    RegistrationIdentityError, validate_device_id, verify_registration_proof,
)

_VERSION = 1
_KEYS = frozenset({"_identity_version", "_device_id", "_last_counter",
                   "ip", "port", "alwayson", "ts"})


class RegistrationStateError(ValueError):
    """Unprovisioned, unsafe, corrupt, or incompatible registration state."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistrationStateError("duplicate JSON field")
        result[key] = value
    return result


def _decode_json(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise RegistrationStateError("invalid registration JSON") from exc
    if not isinstance(obj, dict):
        raise RegistrationStateError("registration JSON must be an object")
    return obj


def _read_state(directory_fd: int) -> tuple[dict | None, tuple[int, int] | None]:
    """Read from the locked directory fd; never interpret corrupt as absent."""
    try:
        fd = os.open("endpoint.json", os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    except FileNotFoundError:
        return None, None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_size > _MAX_BYTES):
            raise RegistrationStateError("unsafe registration file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        after = os.fstat(fd)
        if (len(data) > _MAX_BYTES or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise RegistrationStateError("registration file changed during read")
        current = os.stat("endpoint.json", dir_fd=directory_fd, follow_symlinks=False)
        inode = (before.st_dev, before.st_ino)
        if (current.st_dev, current.st_ino) != inode:
            raise RegistrationStateError("registration file replaced during read")
    finally:
        os.close(fd)

    rec = _decode_json(data)
    if (set(rec) != _KEYS or type(rec["_identity_version"]) is not int or
            rec["_identity_version"] != _VERSION or
            type(rec["_last_counter"]) is not int or
            not 0 <= rec["_last_counter"] < (1 << 63) or
            type(rec["alwayson"]) is not bool or
            not isinstance(rec["ts"], str)):
        raise RegistrationStateError("unprovisioned or invalid registration state")
    try:
        validate_device_id(rec["_device_id"])
        if rec["_last_counter"] == 0:
            if rec["ip"] is not None or rec["port"] is not None or rec["alwayson"]:
                raise RegistrationStateError("invalid initial registration state")
        else:
            ip, port = validate_device_endpoint(rec["ip"], rec["port"])
            if rec["ip"] != ip or type(rec["port"]) is not int or rec["port"] != port:
                raise RegistrationStateError("noncanonical stored endpoint")
    except (ValueError, TypeError) as exc:
        raise RegistrationStateError("invalid registration state") from exc
    return rec, inode


def _publish(directory_fd: int, path: Path, record: dict,
             previous_inode: tuple[int, int] | None) -> None:
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > _MAX_BYTES:
        raise RegistrationStateError("registration record too large")
    name = f".endpoint.json.{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                count = os.write(fd, payload[offset:])
                if count <= 0:
                    raise OSError("short registration write")
                offset += count
            os.fsync(fd)
        finally:
            os.close(fd)
        _assert_journal_directory_identity(path.parent, directory_fd)
        _check_target(directory_fd, "endpoint.json")
        try:
            current = os.stat("endpoint.json", dir_fd=directory_fd,
                              follow_symlinks=False)
            actual = (current.st_dev, current.st_ino)
        except FileNotFoundError:
            actual = None
        if actual != previous_inode:
            raise RegistrationStateError("registration changed outside lock")
        os.replace(name, "endpoint.json", src_dir_fd=directory_fd,
                   dst_dir_fd=directory_fd)
        name = None
        os.fsync(directory_fd)
        _assert_journal_directory_identity(path.parent, directory_fd)
    finally:
        if name is not None:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _require_path(path: Path) -> Path:
    path = Path(path)
    if path.name != "endpoint.json":
        raise ValueError("registration state must be endpoint.json")
    return path


def provision_registration_owner(path: Path, *, owner_id: str,
                                 timeout_seconds: float = 5.0) -> dict:
    """Explicit OFFLINE provisioning only. Never call from an HTTP route.

    Refuses any existing file, including a legacy endpoint. Re-provisioning
    after loss of the counter is a security-sensitive operator action.
    """
    path = _require_path(path)
    owner_id = validate_device_id(owner_id)
    with _endpoint_lock(path, timeout_seconds=timeout_seconds) as directory_fd:
        old, _ = _read_state(directory_fd)
        if old is not None:
            raise RegistrationStateError("registration state already exists")
        rec = {"_identity_version": _VERSION, "_device_id": owner_id,
               "_last_counter": 0, "ip": None, "port": None,
               "alwayson": False, "ts": ""}
        _publish(directory_fd, path, rec, None)
    return rec


def commit_verified_registration(path: Path, *, expected_id: str,
                                 device_id: str, counter: str, signature: str,
                                 body: bytes, credential: bytes, peer_ip: str,
                                 timeout_seconds: float = 5.0) -> dict:
    """Verify and durably publish one signed registration under ONE lock.

    peer_ip MUST be the actual TCP peer from a trusted server, not a forwarded
    header or client JSON. HTTP ACK may be sent only after this returns.
    A failed ACK may still consume a counter after rename; retry with a NEW
    higher counter and fresh signature, never reset/replay the old one.
    """
    path = _require_path(path)
    expected_id = validate_device_id(expected_id)
    if not isinstance(body, bytes) or len(body) > 2048:
        raise RegistrationIdentityError("invalid signed body")
    request_body = _decode_json(body)
    if (set(request_body) - {"port", "alwayson"} or
            type(request_body.get("alwayson")) is not bool):
        raise RegistrationStateError("invalid signed registration fields")
    ip, port = validate_device_endpoint(peer_ip, request_body.get("port", 80))
    with _endpoint_lock(path, timeout_seconds=timeout_seconds) as directory_fd:
        old, inode = _read_state(directory_fd)
        if old is None:
            raise RegistrationStateError("owner not provisioned; no auto-enrollment")
        if not hmac.compare_digest(old["_device_id"].encode("ascii"),
                                   expected_id.encode("ascii")):
            raise RegistrationStateError("stored owner does not match provisioning")
        new_counter = verify_registration_proof(
            expected_id=expected_id, device_id=device_id, counter=counter,
            signature=signature, body=body, credential=credential,
            last_counter=old["_last_counter"])
        rec = {"_identity_version": _VERSION, "_device_id": expected_id,
               "_last_counter": new_counter, "ip": ip, "port": port,
               "alwayson": request_body["alwayson"],
               "ts": datetime.now().isoformat(timespec="seconds")}
        _publish(directory_fd, path, rec, inode)
    return rec
