"""RET-01: batch exact hydrate avoids N+1 get_by_exact_ref."""
from __future__ import annotations

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(agent_id="hongtu", workspace_id="ret01", user_id="darrow")


def test_get_by_exact_refs_matches_single_lookups(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    records = []
    for i in range(5):
        records.append(
            store.append(
                RecordEnvelope.create(
                    kind="memory",
                    title=f"ret01-{i}",
                    summary=f"batch hydrate marker {i}",
                    scope=SCOPE,
                    source="test.ret01",
                    content={"text": f"alpha deployment marker {i}"},
                )
            )
        )
    refs = [
        {"record_id": r.record_id, "scope": r.scope, "source_id": r.source_id}
        for r in records
    ]
    batched = store.get_by_exact_refs(refs)
    assert len(batched) == 5
    by_id = {item.record_id: item for item in batched}
    for record in records:
        single = store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        assert single is not None
        assert by_id[record.record_id].record_id == single.record_id
        assert by_id[record.record_id].status == single.status
    store.close()


def test_get_by_exact_refs_chunking_matches_singles(tmp_path) -> None:
    """RET-07: chunked batch hydrate matches per-ref get_by_exact_ref."""
    store = RuntimeStore(tmp_path)
    records = [
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"ret07-chunk-{i}",
                summary=f"chunk hydrate {i}",
                scope=SCOPE,
                source="test.ret07",
                content={"text": f"chunk marker {i}"},
            )
        )
        for i in range(12)
    ]
    refs = [{"record_id": r.record_id, "scope": r.scope, "source_id": r.source_id} for r in records]
    batched = store.get_by_exact_refs(refs, chunk_size=5)
    assert len(batched) == 12
    for record in records:
        single = store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        assert single is not None
        match = next(item for item in batched if item.record_id == record.record_id)
        assert match.status == single.status
        assert match.source_id == single.source_id
    # Missing refs do not invent rows.
    missing = store.get_by_exact_refs(
        [{"record_id": "no-such", "scope": SCOPE, "source_id": "default"}],
        chunk_size=5,
    )
    assert missing == []
    store.close()
