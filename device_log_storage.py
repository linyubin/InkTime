"""Opaque device-journal filenames with read-only legacy-key compatibility.

The filename digest is an identifier, NOT encryption or an authentication
mechanism. Rotate historically exposed/low-entropy bearer keys separately.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
import stat
import time
try:
    import fcntl
except ImportError:  # POSIX-only; fail closed rather than omit locking.
    fcntl = None
from pathlib import Path
import re

_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)


class DeviceJournalLockTimeout(TimeoutError):
    """Only a bounded flock acquisition timeout, not unrelated I/O timeouts."""


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


def _open_journal_directory(directory: Path, *, create: bool = False) -> int:
    """Open each ancestor with O_DIRECTORY|O_NOFOLLOW and return the owned fd.

    New components are private; existing directory modes are not modified.
    Callers must close the descriptor. Parent traversal and symlinks fail closed.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX journal directory traversal is unavailable")
    directory = Path(directory).expanduser()
    if ".." in directory.parts:
        raise OSError("device journal path cannot contain parent traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(directory.anchor or ".", flags)
    try:
        for part in directory.parts:
            if part in (directory.anchor, ".", "/"):
                continue
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise



def _assert_journal_directory_identity(directory: Path, held_fd: int) -> None:
    """Fail closed if the configured path no longer names the locked inode.

    The held descriptor pins the directory for the full upload transaction.
    Reopening each ancestor without following links detects replacement or
    disappearance; it never follows a substituted symlink.
    """
    try:
        current_fd = _open_journal_directory(directory)
    except OSError as exc:
        raise OSError("device journal directory unavailable while locked") from exc
    try:
        held = os.fstat(held_fd)
        current = os.fstat(current_fd)
        if (not stat.S_ISDIR(held.st_mode)
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError("device journal directory changed while locked")
    finally:
        os.close(current_fd)


def _open_journal_directory_for_operation(
    directory: Path, *, create: bool = False, directory_fd: int | None = None
) -> int:
    """Borrow a dup of the lock-pinned inode, or open independently.

    The caller owns and closes the returned descriptor. A supplied lock fd
    must never be closed by a journal helper; all operations use its inode.
    """
    if directory_fd is None:
        return _open_journal_directory(directory, create=create)
    if type(directory_fd) is not int:
        raise ValueError("device journal directory descriptor must be an integer")
    _assert_journal_directory_identity(directory, directory_fd)
    return os.dup(directory_fd)

@contextmanager
def device_log_lock(path: Path, *, timeout_seconds: float = 5.0):
    """Bounded exclusive cross-process lock for a single opaque device journal.

    The lock file is persistent: unlinking it can split the lock into two
    inodes. Cooperating writers must hold this lock across disk cursor
    recovery, deduplication, append and rotation. Never unlink lock files.
    A contended lock raises TimeoutError; the caller must not ACK uploads.
    """
    if (type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 <= timeout_seconds <= 60):
        raise ValueError("device journal lock timeout must be finite and within 0..60 seconds")
    path = Path(path)
    if not re.fullmatch(r"device-[0-9a-f]{64}\.log", path.name):
        raise ValueError("lock requires an opaque device journal path")
    if fcntl is None or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX device journal locking is unavailable")
    directory_fd = _open_journal_directory(path.parent, create=True)
    try:
        lock_name = path.name + ".lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            _require_regular_single_link(info)
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~0o077)
            # Non-blocking attempts bound the time spent waiting for a writer.
            # The lock remains held for the full caller-owned critical section.
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeviceJournalLockTimeout("device journal lock wait exceeded") from exc
                    time.sleep(min(0.05, remaining))
            try:
                current = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError("device journal lock inode changed")
                _assert_journal_directory_identity(path.parent, directory_fd)
                yield directory_fd
                # A successful HTTP ACK requires that the configured path
                # still resolves to the inode used for every journal action.
                _assert_journal_directory_identity(path.parent, directory_fd)
                final_lock = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                if (final_lock.st_dev, final_lock.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError("device journal lock inode changed during transaction")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def sync_existing_private_device_log(path: Path, *, directory_fd: int | None = None) -> None:
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
        directory_fd = _open_journal_directory_for_operation(path.parent, directory_fd=directory_fd)
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
    path: Path, *, max_tail_bytes: int = 64 * 1024,
    directory_fd: int | None = None,
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
        directory_fd = _open_journal_directory_for_operation(path.parent, directory_fd=directory_fd)
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


def append_private_device_log(
    path: Path, lines: list[str], *, max_bytes: int, directory_fd: int | None = None
) -> None:
    """Append/rotate via a no-follow directory fd, not path-based file opens.

    The caller must hold device_log_lock for cursor recovery and this append.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe POSIX journal writing is unavailable")
    path = Path(path)
    if not lines:
        sync_existing_private_device_log(path, directory_fd=directory_fd)
        return
    directory_fd = _open_journal_directory_for_operation(path.parent, create=True, directory_fd=directory_fd)
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            _require_regular_single_link(info)
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~0o077)
            if info.st_size > max_bytes:
                rotated_name = path.name + ".1"
                try:
                    _require_regular_single_link(
                        os.stat(rotated_name, dir_fd=directory_fd, follow_symlinks=False)
                    )
                except FileNotFoundError:
                    pass
                os.close(fd)
                fd = -1
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError("device journal changed before rotation")
                os.replace(
                    path.name, rotated_name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                fd = os.open(path.name, flags | os.O_EXCL, 0o600, dir_fd=directory_fd)
                _require_regular_single_link(os.fstat(fd))
            stream = os.fdopen(fd, "a", encoding="utf-8")
            fd = -1
            with stream:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory_fd)


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant in device journal")


def read_device_log_lines(
    path: Path, *, max_bytes: int = 16 * 1024 * 1024,
    validate_jsonl: bool = False, directory_fd: int | None = None,
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
        directory_fd = _open_journal_directory_for_operation(path.parent, directory_fd=directory_fd)
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
