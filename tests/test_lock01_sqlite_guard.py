"""LOCK-01: unbound runtime lock must fail closed on guarded mutate paths."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(agent_id="hongtu", workspace_id="lock01")


def test_unbound_lock_fails_closed_on_execute(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "bare.sqlite")
    assert store._runtime_lock is None
    with pytest.raises(RuntimeError, match="sqlite_connection_used_without_runtime_lock"):
        store.execute("SELECT 1")
    store.conn.close()


def test_runtime_store_bind_allows_mutate_under_lock(tmp_path: Path) -> None:
    runtime = RuntimeStore(tmp_path)
    assert runtime.sqlite._runtime_lock is runtime._lock
    record = runtime.append(
        RecordEnvelope.create(
            kind="memory",
            title="lock01",
            summary="lock01",
            scope=SCOPE,
            source="test.lock01",
            content={"text": "hello"},
        )
    )
    assert record.record_id
    runtime.close()


def test_execute_without_owning_lock_fails(tmp_path: Path) -> None:
    runtime = RuntimeStore(tmp_path)
    with pytest.raises(RuntimeError, match="sqlite_connection_used_without_runtime_lock"):
        # Outside runtime._lock — wrapper must reject.
        runtime.sqlite.execute("SELECT 1")
    runtime.close()
