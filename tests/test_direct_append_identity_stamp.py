"""Direct store appends in a canonical Hongtu scope carry identity metadata."""
from __future__ import annotations

from eimemory.identity import needs_hongtu_identity_repair
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from types import SimpleNamespace

SCOPE = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")


def _incident(index: int) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="incident", title=f"timer alert {index}", summary="eimemory-nightly.service:failed",
        content={"text": "timer monitor alert"}, scope=SCOPE, source_id="default",
        source="eimemory.ops.timer_monitor", meta={"report_type": "timer_monitor_alert"},
    )


def test_direct_append_is_stamped_and_not_rewritten_by_nightly_repair(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        record = _incident(1)
        assert needs_hongtu_identity_repair(record)
        before = (record.scope, record.content, record.source, record.time.created_at, record.time.updated_at)
        stored = store.append(record)
        loaded = store.get_by_id(stored.record_id, scope=SCOPE)
        assert not needs_hongtu_identity_repair(loaded)
        assert loaded.meta.get("identity_stamped_on_ingest") is True
        assert (loaded.scope, loaded.content, loaded.source, loaded.time.created_at,
                loaded.time.updated_at) == before
        report = repair_hongtu_identity(SimpleNamespace(store=store), apply=True, scope=SCOPE)
        assert report["repaired_count"] == 0
        assert report["blocked_count"] == 0
    finally:
        store.close()


def test_non_hongtu_scope_is_left_unchanged(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        other = ScopeRef(tenant_id="default", agent_id="xiaomage", workspace_id="other", user_id="darrow")
        record = RecordEnvelope.create(kind="incident", title="x", summary="y", content={"text": "z"},
                                       scope=other, source_id="default", source="eimemory.ops.timer_monitor",
                                       meta={"report_type": "timer_monitor_alert"})
        meta = dict(record.meta)
        stored = store.append(record)
        assert store.get_by_id(stored.record_id, scope=other).meta == meta
    finally:
        store.close()
