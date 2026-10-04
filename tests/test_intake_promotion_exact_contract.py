from copy import deepcopy

import pytest

from eimemory.intake.review import promote_candidate, _deterministic_promoted_memory_id
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id="synthetic", agent_id="agent", workspace_id="workspace", user_id="user")


def candidate(store, **changes):
    values = dict(kind="knowledge_candidate", title="Synthetic", content={"text": "Synthetic fact"},
                  source_id="synthetic-source", scope=SCOPE, status="candidate")
    values.update(changes)
    return store.append(RecordEnvelope.create(**values))


def test_explicit_scope_required(tmp_path):
    store = RuntimeStore(tmp_path)
    row = candidate(store)
    with pytest.raises(ValueError, match="promotion_requires_explicit_scope"):
        promote_candidate(store, row.record_id, "synthetic")
    assert store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).status == "candidate"


def test_partition_and_completed_replay(tmp_path):
    store = RuntimeStore(tmp_path)
    row = candidate(store)
    memory = promote_candidate(store, row.record_id, "synthetic", scope=SCOPE)
    assert memory.source_id == row.source_id
    before = deepcopy(store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).to_dict())
    assert promote_candidate(store, row.record_id, "synthetic", scope=SCOPE).to_dict() == memory.to_dict()
    assert store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).to_dict() == before


@pytest.mark.parametrize("container", ["meta", "provenance", "content", "metadata", "nested_meta"])
@pytest.mark.parametrize("flag", ["prompt_injection", "secret", "secret_detected", "content_redacted"])
def test_reviewed_unsafe_rejected(tmp_path, container, flag):
    store = RuntimeStore(tmp_path)
    changes = {"status": "reviewed"}
    safety = {"safety": {flag: True}}
    if container in ("metadata", "nested_meta"):
        changes["content"] = {"metadata" if container == "metadata" else "meta": safety}
    else:
        changes[container] = safety
    row = candidate(store, **changes)
    before = row.to_dict()
    with pytest.raises(ValueError, match="unsafe_candidate"):
        promote_candidate(store, row.record_id, "approved", note="approved", scope=SCOPE)
    assert store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).to_dict() == before
    assert store.get_by_id(_deterministic_promoted_memory_id(row.record_id), scope=SCOPE, exact_scope=True) is None


@pytest.mark.parametrize("change,error", [
    ({"source_id": "wrong"}, "source_mismatch"),
    ({"kind": "reflection"}, "kind_mismatch"),
    ({"meta": {}}, "lineage_mismatch"),
    ({"provenance": {}}, "lineage_mismatch"),
])
def test_existing_memory_requires_lineage(tmp_path, change, error):
    store = RuntimeStore(tmp_path)
    row = candidate(store, status="promoted")
    values = dict(kind="memory", title="Synthetic memory", source_id=row.source_id, scope=SCOPE,
                  meta={"promoted_from": row.record_id}, provenance={"promoted_from": row.record_id})
    values.update(change)
    memory = RecordEnvelope.create(**values)
    memory.record_id = _deterministic_promoted_memory_id(row.record_id)
    store.append(memory)
    with pytest.raises(ValueError, match=error):
        promote_candidate(store, row.record_id, "synthetic", scope=SCOPE)


def test_atomic_recheck_rejects_changed_candidate(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    row = candidate(store)
    original = store.mutate_records_atomically
    def mutate(callback):
        current = store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id)
        current.summary = "Concurrent synthetic change"
        store.append(current)
        return original(callback)
    monkeypatch.setattr(store, "mutate_records_atomically", mutate)
    with pytest.raises(ValueError, match="candidate_changed"):
        promote_candidate(store, row.record_id, "synthetic", scope=SCOPE)
    assert store.get_by_id(_deterministic_promoted_memory_id(row.record_id), scope=SCOPE, exact_scope=True) is None
    assert store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).summary == "Concurrent synthetic change"


def test_atomic_second_write_failure_rolls_back(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    row = candidate(store)
    original = store.sqlite.upsert
    def upsert(value, **kwargs):
        if value.kind == "memory":
            raise OSError("synthetic write failure")
        return original(value, **kwargs)
    monkeypatch.setattr(store.sqlite, "upsert", upsert)
    with pytest.raises(RuntimeError, match="atomic_write_outcome_unknown"):
        promote_candidate(store, row.record_id, "synthetic", scope=SCOPE)
    assert store.get_by_exact_ref(row.record_id, scope=SCOPE, source_id=row.source_id).status == "candidate"
    assert store.get_by_id(_deterministic_promoted_memory_id(row.record_id), scope=SCOPE, exact_scope=True) is None
