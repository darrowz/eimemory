from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import errno
import json
from math import isfinite
import os
from pathlib import Path
import stat
import tempfile
import threading
from time import monotonic, sleep
from typing import Any, BinaryIO, Callable, Iterator
from weakref import WeakValueDictionary

from eimemory.core.strict_json import loads as strict_json_loads


DEFAULT_MAX_JSON_STATE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_JSON_STATE_DEPTH = 64
# Active holders/waiters retain their lock. Historical paths must not retain
# locks forever in a long-running agent process.
_LOCAL_LOCKS: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_LOCAL_LOCKS_GUARD = threading.Lock()
_MISSING = object()


def _local_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _regular_stat(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("state file must be a single-link regular file")


def _existing_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _regular_stat(info)
    return info


@contextmanager
def open_regular_binary(path: str | Path, *, create: bool = False) -> Iterator[BinaryIO]:
    """Open and validate the file descriptor before reading or writing bytes.

    The leaf must be a single-link regular file, not a symlink/FIFO/device.
    Parent directories must be trusted; this is not an adversarial-directory
    sandbox. O_NONBLOCK prevents a substituted FIFO from hanging during open.
    """
    target = Path(path)
    _existing_stat(target)
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        _regular_stat(opened)
        named = target.lstat()
        _regular_stat(named)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("state file changed during open")
        handle = os.fdopen(descriptor, "r+b" if create else "rb")
        descriptor = -1  # ownership transferred to handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    with handle:
        yield handle


def _prepare_lock_byte(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


if os.name == "nt":
    import msvcrt

    def _lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _try_lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _try_lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_until(handle: Any, deadline: float | None) -> None:
    if deadline is None:
        _lock(handle)
        return
    while True:
        try:
            _try_lock(handle)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("state_lock_timeout") from None
            sleep(min(0.01, remaining))


@contextmanager
def interprocess_lock(path: str | Path, *, timeout: float | None = None) -> Iterator[None]:
    """Serialize participating writers, optionally bounding lock wait time.

    The timeout covers local/interprocess lock acquisition, not filesystem I/O
    or the caller's critical section. None retains the existing blocking API.
    """
    if timeout is not None:
        timeout = float(timeout)
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("lock timeout must be finite and nonnegative")
    deadline = None if timeout is None else monotonic() + timeout
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local = _local_lock(lock_path)
    acquired = local.acquire() if deadline is None else local.acquire(
        timeout=min(threading.TIMEOUT_MAX, max(0.0, deadline - monotonic()))
    )
    if not acquired:
        raise TimeoutError("state_lock_timeout")
    try:
        with open_regular_binary(lock_path, create=True) as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            _prepare_lock_byte(handle)
            _lock_until(handle, deadline)
            try:
                yield
            finally:
                _unlock(handle)
    finally:
        local.release()


def _expected_name(expected_type: type[Any]) -> str:
    return {dict: "object", list: "array"}.get(expected_type, expected_type.__name__)


def _check_limits(max_bytes: int, max_depth: int) -> None:
    if type(max_bytes) is not int or max_bytes < 1 or type(max_depth) is not int or max_depth < 1:
        raise ValueError("JSON limits must be positive integers")


def read_json_strict(
    path: str | Path, expected_type: type[Any], *,
    max_bytes: int = DEFAULT_MAX_JSON_STATE_BYTES,
    max_depth: int = DEFAULT_MAX_JSON_STATE_DEPTH,
) -> Any:
    _check_limits(max_bytes, max_depth)
    target = Path(path)
    try:
        with open_regular_binary(target) as handle:
            if os.fstat(handle.fileno()).st_size > max_bytes:
                raise ValueError("json_too_large")
            raw = handle.read(max_bytes + 1)
        payload = strict_json_loads(raw, max_bytes=max_bytes, max_depth=max_depth)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON state at {target}") from exc
    if not isinstance(payload, expected_type):
        raise ValueError(
            f"invalid JSON state at {target}: expected {_expected_name(expected_type)}"
        )
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Atomically publish private bytes; callers own any read/modify/write lock.

    An error from directory fsync occurs after publication and therefore means
    durability is uncertain, not that the previous bytes remain installed.
    """
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = _existing_stat(target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        try:
            handle = os.fdopen(descriptor, "wb")
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        with handle:
            handle.write(payload)
            handle.flush()
            if os.name == "posix":
                current_stat = os.fstat(handle.fileno())
                if existing_stat is not None and (
                    current_stat.st_uid, current_stat.st_gid
                ) != (existing_stat.st_uid, existing_stat.st_gid):
                    os.fchown(handle.fileno(), existing_stat.st_uid, existing_stat.st_gid)
                os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        _existing_stat(target)  # reject unsafe leaf substitution before publish
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(
    path: str | Path, payload: Any, *,
    max_bytes: int = DEFAULT_MAX_JSON_STATE_BYTES,
    max_depth: int = DEFAULT_MAX_JSON_STATE_DEPTH,
) -> None:
    _check_limits(max_bytes, max_depth)
    # Validate before creating a directory or temporary file. Writer and reader
    # accept the same JSON language, including depth and duplicate-key limits.
    encoded = bytearray()
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        for chunk in encoder.iterencode(payload):
            encoded.extend(chunk.encode("utf-8"))
            if len(encoded) + 1 > max_bytes:
                raise ValueError("json_too_large")
        encoded.extend(b"\n")
        raw = bytes(encoded)
        strict_json_loads(raw, max_bytes=max_bytes, max_depth=max_depth)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid JSON state payload") from None
    atomic_write_bytes(path, raw)


def locked_json_update(
    path: str | Path,
    mutate: Callable[[Any], Any],
    *,
    default: Any = _MISSING,
    expected_type: type[Any] = dict,
    max_bytes: int = DEFAULT_MAX_JSON_STATE_BYTES,
    max_depth: int = DEFAULT_MAX_JSON_STATE_DEPTH,
) -> Any:
    _check_limits(max_bytes, max_depth)
    target = Path(path)
    lock_path = target.with_name(f"{target.name}.lock")
    with interprocess_lock(lock_path):
        if os.path.lexists(target):
            current = read_json_strict(target, expected_type, max_bytes=max_bytes, max_depth=max_depth)
        elif default is not _MISSING:
            current = deepcopy(default)
            if not isinstance(current, expected_type):
                raise TypeError("default does not match expected_type")
        else:
            raise FileNotFoundError(target)
        updated = mutate(current)
        if not isinstance(updated, expected_type):
            raise TypeError("mutate result does not match expected_type")
        atomic_write_json(target, updated, max_bytes=max_bytes, max_depth=max_depth)
        return updated
