"""Request-owned SQLite read deadlines, serialized by RuntimeStore's lock."""
from contextlib import contextmanager
from math import isfinite
import sqlite3
from time import monotonic


class RecallReadDeadlineExceeded(TimeoutError):
    pass


def incomplete_recall_report():
    return {'retrieval_mode': 'deadline_exhausted', 'candidate_count': 0,
            'vector_hits': 0, 'scored_items': [],
            'blocked_counts': {'recall_budget_exhausted': 1}}


def _deadline(filters):
    try:
        value = float((filters or {}).get('_recall_collection_deadline_monotonic') or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if isfinite(value) and value > 0 else 0.0


class _ReadConnection:
    """Refresh busy waiting before each statement, not just at scope entry."""

    def __init__(self, connection, deadline):
        self.raw = connection
        self.deadline = deadline

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def prepare(self):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise RecallReadDeadlineExceeded('recall_budget_exhausted')
        self.raw.execute(f'PRAGMA busy_timeout = {max(0, int(remaining * 1000))}')

    def execute(self, sql, parameters=()):
        self.prepare()
        return self.raw.execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.prepare()
        return self.raw.executemany(sql, parameters)

    def cursor(self, *args, **kwargs):
        return _ReadCursor(self, self.raw.cursor(*args, **kwargs))


class _ReadCursor:
    def __init__(self, connection, cursor):
        self.connection = connection
        self.raw = cursor

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __iter__(self):
        return iter(self.raw)

    def execute(self, sql, parameters=()):
        self.connection.prepare()
        self.raw.execute(sql, parameters)
        return self

    def executemany(self, sql, parameters):
        self.connection.prepare()
        self.raw.executemany(sql, parameters)
        return self


@contextmanager
def recall_read_scope(store, filters):
    deadline = _deadline(filters)
    remaining = deadline - monotonic() if deadline else None
    if remaining is not None and remaining <= 0:
        raise RecallReadDeadlineExceeded('recall_budget_exhausted')
    acquired = store._lock.acquire(timeout=remaining) if remaining is not None else store._lock.acquire()
    if not acquired:
        raise RecallReadDeadlineExceeded('recall_budget_exhausted')
    try:
        if not deadline:
            yield
            return
        owner = store.sqlite
        previous_connection = owner.conn
        connection = previous_connection.raw if isinstance(previous_connection, _ReadConnection) else previous_connection
        if isinstance(previous_connection, _ReadConnection):
            deadline = min(deadline, previous_connection.deadline)
        previous_busy = connection.execute('PRAGMA busy_timeout').fetchone()[0]
        previous_progress = getattr(owner, '_recall_read_progress', None)
        progress = lambda: int(monotonic() >= deadline)
        owner._recall_read_progress = progress
        owner.conn = _ReadConnection(connection, deadline)
        connection.set_progress_handler(progress, 1000)
        try:
            yield
        except sqlite3.OperationalError as exc:
            # SQLite floors its busy timeout to milliseconds. Do not convert
            # unrelated SQL failures or externally interrupted reads to budget.
            code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
            if code in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and monotonic() >= deadline - .001:
                raise RecallReadDeadlineExceeded('recall_budget_exhausted') from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)
            owner.conn = previous_connection
            owner._recall_read_progress = previous_progress
            connection.execute(f'PRAGMA busy_timeout = {int(previous_busy)}')
            if previous_progress is not None:
                connection.set_progress_handler(previous_progress, 1000)
    finally:
        store._lock.release()
