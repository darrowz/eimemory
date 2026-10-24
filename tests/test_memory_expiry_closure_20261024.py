"""Every elapsed validity bound restricts a memory's current recall authority."""
from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="audit", agent_id="memory", workspace_id="expiry", user_id="owner")
PAST = "2000-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


@pytest.mark.parametrize("bounds", [
    {"invalid_at": FUTURE, "valid_until": PAST},
    {"invalid_at": "unknown", "valid_until": PAST},
    {"invalid_at": FUTURE, "effective_until": PAST},
    {"valid_until": FUTURE, "effective_until": PAST},
])
@pytest.mark.parametrize("nested", [False, True])
def test_expired_bound_is_not_hidden_by_another_bound(tmp_path, bounds, nested):
    with closing(RuntimeStore(tmp_path)) as store:
        metadata = {"memory_type": "fact", "quality": {"capture_decision": "accept"},
                    **({"business_meta": bounds} if nested else bounds)}
        expired = store.append(RecordEnvelope.create(kind="memory", title="Archive routing destination",
            summary="Archive routing destination is the retired endpoint.",
            content={"text": "Archive routing destination is the retired endpoint.", "memory_type": "fact"},
            meta=metadata, scope=SCOPE, source="test", source_id="test"))
        current = store.append(RecordEnvelope.create(kind="memory", title="Archive routing destination",
            summary="Archive routing destination is the current endpoint.",
            content={"text": "Archive routing destination is the current endpoint.", "memory_type": "fact"},
            meta={"memory_type": "fact", "quality": {"capture_decision": "accept"}},
            scope=SCOPE, source="test", source_id="test"))
        memory = MemoryAPI(store)
        assert memory._is_temporally_stale_memory(expired) is True
        for operational in (False, True):
            bundle = memory.recall(query="Archive routing destination", scope=asdict(SCOPE), limit=5,
                task_context={"source_ids": ["test"], "exact_scope_only": True,
                              "include_operational_recall": operational})
            assert [item.record_id for item in bundle.items] == [current.record_id]


def test_independent_future_bounds_do_not_expire_current_memory():
    record = RecordEnvelope.create(kind="memory", title="Current fact", scope=SCOPE,
        meta={"invalid_at": FUTURE, "valid_until": FUTURE, "effective_until": FUTURE})
    assert MemoryAPI._is_temporally_stale_memory(record) is False
