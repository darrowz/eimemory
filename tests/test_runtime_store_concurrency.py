from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from eimemory.storage.runtime_store import RuntimeStore
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_runtime_store_serializes_concurrent_writes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="personal")

    def write_once(idx: int) -> str:
        record = RecordEnvelope.create(
            kind="memory",
            title=f"Concurrent memory {idx}",
            summary="Concurrent write should be serialized",
            scope=scope,
            source="test",
            content={"text": f"memory {idx}"},
            meta={"idx": idx},
        )
        store.append(record)
        store.record_event({"event_type": "test.concurrent", "source_record_id": record.record_id}, scope=scope)
        return record.record_id

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            record_ids = list(pool.map(write_once, range(40)))
        memories = store.list_records(kinds=["memory"], scope=scope, limit=100)
    finally:
        store.close()

    assert len(set(record_ids)) == 40
    assert len(memories) == 40
