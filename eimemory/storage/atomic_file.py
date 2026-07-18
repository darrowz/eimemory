from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
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

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def interprocess_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ValueError(f"lock file must not be a symlink: {lock_path}")
    with _local_lock(lock_path):
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            _prepare_lock_byte(handle)
            _lock(handle)
            try:
                yield
            finally:
                _unlock(handle)


def _expected_name(expected_type: type[Any]) -> str:
    return {dict: "object", list: "array"}.get(expected_type, expected_type.__name__)


def read_json_strict(path: str | Path, expected_type: type[Any]) -> Any:
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"JSON state must not be a symlink: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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


def atomic_write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"JSON state must not be a symlink: {target}")
    existing_stat = target.stat(follow_symlinks=False) if target.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        mode = existing_stat.st_mode & 0o777 if existing_stat is not None else 0o600
        os.chmod(temporary, mode)
        if os.name == "posix" and existing_stat is not None:
            os.chown(temporary, existing_stat.st_uid, existing_stat.st_gid)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def locked_json_update(
    path: str | Path,
    mutate: Callable[[Any], Any],
    *,
    default: Any = _MISSING,
    expected_type: type[Any] = dict,
) -> Any:
    target = Path(path)
    lock_path = target.with_name(f"{target.name}.lock")
    with interprocess_lock(lock_path):
        if target.exists():
            current = read_json_strict(target, expected_type)
        elif default is not _MISSING:
            current = deepcopy(default)
            if not isinstance(current, expected_type):
                raise TypeError("default does not match expected_type")
        else:
            raise FileNotFoundError(target)
        updated = mutate(current)
        if not isinstance(updated, expected_type):
            raise TypeError("mutate result does not match expected_type")
        atomic_write_json(target, updated)
        return updated
