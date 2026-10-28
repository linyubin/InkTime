"""Opaque device-journal filenames with read-only legacy-key compatibility.

The filename digest is an identifier, NOT encryption or an authentication
mechanism. Rotate historically exposed/low-entropy bearer keys separately.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import stat
try:
    import fcntl
except ImportError:  # POSIX-only; fail closed rather than omit locking.
    fcntl = None
from pathlib import Path
import re

_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)


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


def _require_regular_single_link(info) -> None:
    """Refuse symlinks, special files and hard-linked journal targets."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("device journal target is not a single-link regular file")


@contextmanager
def device_log_lock(path: Path):
    """Exclusive cross-process lock for a single opaque device journal.

    The lock file is persistent: unlinking it can split the lock into two
    inodes. Cooperating writers must hold this lock across disk cursor
    recovery, deduplication, append and rotation.
    """
    path = Path(path)
    if not re.fullmatch(r"device-[0-9a-f]{64}\.log", path.name):
        raise ValueError("lock requires an opaque device journal path")
    if fcntl is None or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX device journal locking is unavailable")
    directory = path.parent
    if directory.is_symlink():
        raise OSError("device journal directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(directory, dir_flags)
    try:
        lock_name = path.name + ".lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            _require_regular_single_link(info)
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~0o077)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                current = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError("device journal lock inode changed")
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def sync_existing_private_device_log(path: Path) -> None:
    """Durably sync existing opaque journal generations before duplicate ACK.

    A failed fsync may leave already-appended bytes visible in the page cache.
    On a retry, the server can observe those bytes and filter all lines as
    duplicates; that retry MUST sync the visible records before returning 200.
    Missing journal generations are not created.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX journal fsync is unavailable")
    path = Path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(path.parent, flags)
    except FileNotFoundError:
        return
    try:
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        for name in (path.name + ".1", path.name):
            try:
                fd = os.open(name, file_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            try:
                _require_regular_single_link(os.fstat(fd))
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)



def recover_incomplete_private_device_log_tail(
    path: Path, *, max_tail_bytes: int = 64 * 1024
) -> int:
    """Remove an unacknowledged, unterminated JSONL tail before cursor recovery.

    The caller MUST hold device_log_lock(path). The server only acknowledges
    newline-terminated records after fsync, so an unterminated final record
    cannot have been successfully ACKed by the current writer. Preserve every
    preceding complete line, including one whose fsync previously failed.
    Oversized fragments fail closed rather than silently discarding data.
    Never repair legacy plaintext-key files or rotated generations.
    """
    path = Path(path)
    if not re.fullmatch(r"device-[0-9a-f]{64}\.log", path.name):
        raise ValueError("recovery requires an opaque active journal")
    if type(max_tail_bytes) is not int or max_tail_bytes < 1:
        raise ValueError("max_tail_bytes must be positive")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "pread"):
        raise OSError("safe POSIX journal recovery is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except FileNotFoundError:
        return 0
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return 0
        try:
            info = os.fstat(fd)
            _require_regular_single_link(info)
            size = info.st_size
            if not size:
                return 0
            last = os.pread(fd, 1, size - 1)
            if last == b"\n":
                return 0
            if len(last) != 1:
                raise OSError("journal changed during tail inspection")
            length = min(size, max_tail_bytes)
            tail = os.pread(fd, length, size - length)
            if len(tail) != length:
                raise OSError("journal changed during tail inspection")
            last_newline = tail.rfind(b"\n")
            if last_newline < 0 and size > max_tail_bytes:
                raise OSError("unterminated journal tail exceeds safe recovery limit")
            offset = size - length + last_newline + 1 if last_newline >= 0 else 0
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or os.fstat(fd).st_size != size):
                raise OSError("journal changed before tail recovery")
            os.ftruncate(fd, offset)
            # Recovery is not acknowledged until both truncation and its
            # directory metadata are synchronized. Retry is safe after failure.
            os.fsync(fd)
            os.fsync(directory_fd)
            return size - offset
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def append_private_device_log(path: Path, lines: list[str], *, max_bytes: int) -> None:
    """Append JSONL with private permissions; fail closed on unsafe targets.

    Existing regular files with permissive modes are tightened in place.
    Rotation retains the previous generation. Concurrent callers must hold
    device_log_lock(path) around disk cursor recovery and this entire append.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX journal writing is unavailable")
    path = Path(path)
    if not lines:
        sync_existing_private_device_log(path)
        return
    directory = path.parent
    if directory.is_symlink():
        raise OSError("device journal directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        _require_regular_single_link(info)
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~0o077)
        if info.st_size > max_bytes:
            rotated = path.with_suffix(".log.1")
            try:
                _require_regular_single_link(os.lstat(rotated))
            except FileNotFoundError:
                pass
            os.close(fd)
            fd = -1
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise OSError("device journal changed before rotation")
            # Do not follow symlinks at either path. Cooperating callers
            # serialize this rename through device_log_lock.
            os.replace(path, rotated)
            fd = os.open(path, flags | os.O_EXCL, 0o600)
            _require_regular_single_link(os.fstat(fd))
        stream = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        with stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            # A successful HTTP 200 must not precede file-data durability.
            os.fsync(stream.fileno())
        # Also persist the newly created/rotated directory entries. Failure
        # propagates to the handler (500); retry will sync duplicates again.
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        dir_fd = os.open(directory, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)

def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant in device journal")


def read_device_log_lines(
    path: Path, *, max_bytes: int = 16 * 1024 * 1024,
    validate_jsonl: bool = False,
) -> list[str] | None:
    """Read an existing journal without following a symlink or hardlink.

    None means absent; unsafe/unreadable/oversized journals raise OSError.
    The directory is opened no-follow and the basename is resolved relative
    to that directory descriptor to avoid file-level path substitution.
    With validate_jsonl=True (for upload cursor recovery), reject any incomplete,
    malformed or non-object record rather than silently losing an acknowledged
    sequence number. This includes rotated and legacy generations. No journal
    contents or directory entries are modified by this reader.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe no-follow journal reads are unavailable")
    path = Path(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except FileNotFoundError:
        return None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            _require_regular_single_link(info)
            if info.st_size > max_bytes:
                raise OSError("device journal exceeds safe read limit")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                payload = stream.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise OSError("device journal grew beyond safe read limit")
            if not validate_jsonl:
                return payload.decode("utf-8", errors="replace").splitlines()
            if payload and not payload.endswith(b"\n"):
                raise OSError("device journal has an unterminated record")
            try:
                decoded = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise OSError("device journal has invalid UTF-8") from exc
            # Split only on JSONL LF, not on Unicode separators within strings.
            lines = decoded.split("\n")[:-1] if payload else []
            for line in lines:
                try:
                    record = json.loads(
                        line, parse_constant=_reject_nonstandard_json_constant
                    )
                except (ValueError, TypeError) as exc:
                    raise OSError("device journal contains malformed JSONL") from exc
                if (not isinstance(record, dict)
                        or type(record.get("ls")) is not int
                        or record["ls"] < 1):
                    raise OSError("device journal has an invalid sequence number")
            return lines
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory_fd)
