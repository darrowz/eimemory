"""One absolute budget for projection maintenance and its pure remote work."""
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar
from math import isfinite
from threading import BoundedSemaphore, Event, Lock, Timer
from time import monotonic
import sqlite3
import socket
import sys
from uuid import uuid4


class ProjectionMaintenanceDeadlineExceeded(TimeoutError):
    def __init__(self, *, pending: Future | None = None):
        super().__init__('projection_maintenance_deadline_exhausted')
        self.pending = pending


class ProjectionCommitOutcomeUnknown(RuntimeError):
    def __init__(self):
        super().__init__('projection_commit_outcome_unknown')


class ProjectionDeadlineTransportUnsupported(RuntimeError):
    def __init__(self):
        super().__init__('projection_maintenance_deadline_transport_unsupported')


class DeadlineSocket:
    """Interrupt only a maintenance connection's own libpq socket at expiry."""
    def __init__(self, connection, deadline_at):
        self.expired = Event()
        self._lock = Lock()
        self._socket = None
        self._timer = None
        pgconn = getattr(connection, 'pgconn', None)
        if pgconn is None:
            return
        # Winsock duplicates share shutdown state, but shutdown does not wake
        # select() on the original handle. Do not promise a hard deadline there.
        # Unbounded serving/maintenance never constructs this watchdog.
        if sys.platform == 'win32':
            raise ProjectionDeadlineTransportUnsupported()
        # A duplicate retains this exact socket even if its original descriptor
        # number is later reused. It never shuts down a serving pool connection.
        duplicate = socket.dup(pgconn.socket)
        try:
            self._socket = socket.socket(fileno=duplicate)
        except BaseException:
            socket.close(duplicate)
            raise
        self._timer = Timer(max(0.0, deadline_at - monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            self.expired.set()
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._socket.close()
                self._socket = None

    def close(self):
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None


# A late pure operation retains its slot until it really exits, including when
# a subsequent maintenance invocation constructs a new provider/repository.
_pure_slots = BoundedSemaphore(2)
_operation_owner = ContextVar('projection_maintenance_owner', default=None)
_pure_running = ContextVar('projection_maintenance_pure_work', default=False)


@contextmanager
def projection_operation():
    owner = _operation_owner.get()
    token = _operation_owner.set(owner or 'worker-' + uuid4().hex)
    try:
        yield
    finally:
        _operation_owner.reset(token)


def maintenance_owner():
    return _operation_owner.get() or 'worker-' + uuid4().hex


def validate_deadline(deadline_at):
    if deadline_at is None:
        return None
    if type(deadline_at) not in (int, float) or not isfinite(deadline_at) or deadline_at <= 0:
        raise ValueError('projection_maintenance_deadline_invalid')
    return float(deadline_at)


def remaining_seconds(deadline_at, maximum=None):
    remaining = float('inf') if not deadline_at else deadline_at - monotonic()
    if remaining <= 0:
        raise ProjectionMaintenanceDeadlineExceeded()
    return remaining if maximum is None else min(remaining, maximum)


def sqlite_deadline_exceeded(exc, deadline_at):
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded
    if isinstance(exc, RecallReadDeadlineExceeded):
        return True
    code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
    return (bool(deadline_at) and isinstance(exc, sqlite3.OperationalError)
            and code in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            and monotonic() >= deadline_at - 0.001)


def pure_call(function, *args, deadline_at=None, **kwargs):
    """Bound pure embeddings/connects; this function never runs a PG write."""
    if not deadline_at:
        return function(*args, **kwargs)
    remaining_seconds(deadline_at)
    if _pure_running.get():
        # A bounded read owns its connection setup and cleanup. A nested pure
        # call stays in that same worker instead of consuming another slot.
        return function(*args, **kwargs)
    if not _pure_slots.acquire(timeout=remaining_seconds(deadline_at)):
        raise ProjectionMaintenanceDeadlineExceeded()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        def run():
            token = _pure_running.set(True)
            try:
                return function(*args, **kwargs)
            finally:
                _pure_running.reset(token)
        pending = executor.submit(run)
    except BaseException:
        _pure_slots.release()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    pending.add_done_callback(lambda _: _pure_slots.release())
    try:
        try:
            try:
                wait_seconds = remaining_seconds(deadline_at)
            except ProjectionMaintenanceDeadlineExceeded:
                raise ProjectionMaintenanceDeadlineExceeded(pending=pending) from None
            result = pending.result(timeout=wait_seconds)
        except FutureTimeout:
            # An early backend timeout keeps its original failure semantics.
            if pending.done():
                failure = pending.exception(timeout=0)
                if failure is not None:
                    raise failure
            raise ProjectionMaintenanceDeadlineExceeded(pending=pending) from None
        if monotonic() >= deadline_at:
            raise ProjectionMaintenanceDeadlineExceeded(pending=pending)
        return result
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@contextmanager
def sqlite_scope(store, deadline_at):
    if not deadline_at:
        yield
        return
    from eimemory.storage.recall_deadline import recall_read_scope, RecallReadDeadlineExceeded
    try:
        with recall_read_scope(store, {'_recall_collection_deadline_monotonic': deadline_at}):
            yield
        remaining_seconds(deadline_at)
    except RecallReadDeadlineExceeded:
        raise ProjectionMaintenanceDeadlineExceeded() from None
