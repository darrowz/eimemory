"""Serialize complete controlled acceptance windows on the SQLite authority.

The lock protects the existing newest-attempt rule; it grants no acceptance or
permission. A closure holds it through its final independent readback, while a
nightly writer holds the same lock through execution and verification.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import isfinite
import os
from pathlib import Path
import stat
import threading

from eimemory.storage.atomic_file import interprocess_lock

DEFAULT_WAIT_SECONDS = 600.0
_HELD = threading.local()


class ControlledAcceptanceWindowError(RuntimeError):
    pass


def _authority(runtime):
    try:
        value = runtime.store.sqlite.path
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            raise ValueError('missing database path')
        named = Path(value)
        if named.is_symlink():
            raise ValueError('database symlink')
        path = named.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('database is not a single physical file')
        return path, (info.st_dev, info.st_ino)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ControlledAcceptanceWindowError('controlled_acceptance_window_authority_unavailable') from exc


@contextmanager
def controlled_acceptance_window(runtime, *, timeout_seconds=DEFAULT_WAIT_SECONDS):
    """Bound acquisition, reuse only this thread/process's matching authority.

    Real stores never silently bypass this fence. Different callers/scopes use
    the same authority lock because latest attempts and mutable source versions
    can affect the complete release workflow. The OS lock remains held through
    nested CPA execution inside a closure; another thread/process must wait.
    """
    if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds)
            or not 0 <= timeout_seconds <= DEFAULT_WAIT_SECONDS):
        raise ValueError('controlled_acceptance_window_timeout_invalid')
    path, identity = _authority(runtime)
    held = getattr(_HELD, 'windows', None)
    if held is None or getattr(_HELD, 'pid', None) != os.getpid():
        held = _HELD.windows = {}
        _HELD.pid = os.getpid()
    key = str(path)
    if key in held:
        if held[key] != identity:
            raise ControlledAcceptanceWindowError('controlled_acceptance_window_authority_replaced')
        yield
        return
    lock = path.with_name('.' + path.name + '.controlled-acceptance-window.lock')
    entered = False
    try:
        with interprocess_lock(lock, timeout=timeout_seconds):
            entered = True
            if _authority(runtime) != (path, identity):
                raise ControlledAcceptanceWindowError('controlled_acceptance_window_authority_replaced')
            held[key] = identity
            try:
                yield
                if _authority(runtime) != (path, identity):
                    raise ControlledAcceptanceWindowError('controlled_acceptance_window_authority_replaced')
            finally:
                held.pop(key, None)
    except TimeoutError as exc:
        if entered:
            raise
        raise ControlledAcceptanceWindowError('controlled_acceptance_window_busy') from exc
    except (OSError, ValueError) as exc:
        if entered:
            raise
        raise ControlledAcceptanceWindowError('controlled_acceptance_window_unavailable') from exc
