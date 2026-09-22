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

def test_maintain_requires_runtime_lock(tmp_path: Path) -> None:
    """LOCK-01: maintenance PRAGMA/vacuum path is fail-closed without lock ownership."""
    store = SqliteRecordStore(tmp_path / "bare.sqlite")
    with pytest.raises(RuntimeError, match="sqlite_connection_used_without_runtime_lock"):
        store.maintain()
    store.conn.close()


def test_runtime_maintain_and_rebuild_hold_lock(tmp_path: Path) -> None:
    """LOCK-01: RuntimeStore maintain + rebuild keep the lock across PRAGMA paths."""
    runtime = RuntimeStore(tmp_path)
    runtime.append(
        RecordEnvelope.create(
            kind="memory",
            title="lock01-rebuild",
            summary="lock01-rebuild",
            scope=SCOPE,
            source="test.lock01",
            content={"text": "rebuild me"},
        )
    )
    held: list[bool] = []
    original_assert = runtime.sqlite.assert_connection_lock_held

    def _tracking_assert() -> None:
        owned = runtime._lock._is_owned() if hasattr(runtime._lock, "_is_owned") else True
        held.append(bool(owned))
        return original_assert()

    runtime.sqlite.assert_connection_lock_held = _tracking_assert  # type: ignore[method-assign]
    report = runtime.maintain_storage()
    assert report.get("ok") is True
    assert held, "maintain must invoke lock assertion"
    assert all(held), "maintain must hold lock for every PRAGMA/execute"

    held.clear()
    rebuild = runtime.rebuild_sqlite_from_jsonl(replace=False)
    assert rebuild.get("ok") is True
    assert held, "rebuild must invoke lock assertion"
    assert all(held), "rebuild must hold lock for every PRAGMA/execute"
    runtime.close()
