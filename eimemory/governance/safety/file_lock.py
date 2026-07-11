from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` for a small file update."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local_lock = _local_lock(lock_path)
    with local_lock:
        with lock_path.open("a+b") as handle:
            _prepare_lock_byte(handle)
            _lock(handle)
            try:
                yield
            finally:
                _unlock(handle)


def _local_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _prepare_lock_byte(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
