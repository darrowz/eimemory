"""Locked read access to the runtime SQLite connection for non-storage packages.

Code outside ``eimemory.storage`` must not touch ``store._lock`` or
``store.sqlite.conn`` directly; that couples it to the single-connection +
RLock layout and silently breaks when the connection model changes.
"""

from __future__ import annotations

from typing import Any, Iterable


class StoreUnavailable(RuntimeError):
    """The owner exposes no SQLite-backed store."""


def _store(owner: Any) -> Any:
    store = getattr(owner, "store", None)
    return owner if store is None else store


def _raw_connection(store: Any) -> Any:
    sqlite = getattr(store, "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        conn = getattr(store, "conn", None)
    return conn


def store_available(owner: Any) -> bool:
    return _raw_connection(_store(owner)) is not None


def locked_read(owner: Any, sql: str, params: Iterable[Any] = (), *, one: bool = False) -> Any:
    """Run one read statement under the RuntimeStore lock and fetch its rows.

    ``owner`` may be a Runtime, a RuntimeStore, or a test double exposing
    ``conn`` / ``sqlite.conn``.  Raises :class:`StoreUnavailable` when there is
    no connection at all.
    """
    store = _store(owner)
    bound = tuple(params)

    def execute(conn: Any) -> Any:
        cursor = conn.execute(sql, bound)
        return cursor.fetchone() if one else cursor.fetchall()

    runner = getattr(store, "run_locked", None)
    if callable(runner) and getattr(getattr(store, "sqlite", None), "conn", None) is not None:
        return runner(lambda sqlite: execute(sqlite.conn))
    conn = _raw_connection(store)
    if conn is None:
        raise StoreUnavailable("sqlite_store_unavailable")
    return execute(conn)


class _FetchedCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class LockedConnection:
    """Read-only connection facade: each ``execute`` runs and fetches under the lock."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _FetchedCursor:
        return _FetchedCursor(list(locked_read(self._owner, sql, params)))

    @property
    def total_changes(self) -> int:
        return total_changes(self._owner)


def locked_connection(owner: Any) -> LockedConnection | None:
    return LockedConnection(owner) if store_available(owner) else None


def total_changes(owner: Any) -> int:
    store = _store(owner)
    runner = getattr(store, "run_locked", None)
    if callable(runner) and getattr(getattr(store, "sqlite", None), "conn", None) is not None:
        return int(runner(lambda sqlite: sqlite.conn.total_changes))
    conn = _raw_connection(store)
    if conn is None:
        raise StoreUnavailable("sqlite_store_unavailable")
    return int(conn.total_changes)


__all__ = [
    "LockedConnection",
    "StoreUnavailable",
    "locked_connection",
    "locked_read",
    "store_available",
    "total_changes",
]
