from __future__ import annotations

from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.mark.parametrize("backend", ["runtime", "sqlite"])
def test_latest_evidence_preserves_scope_source_status_and_insertion_order(tmp_path, backend) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")
    source = "eimemory.live_task_acceptance"

    def append(*, status="active", record_scope=scope, record_source=source,
               kind="learning_eval", key="case"):
        record = RecordEnvelope.create(
            kind=kind, title="Native high-water evidence", scope=record_scope,
            source=record_source, status=status, meta={"observation_key": key},
        )
        record.time.created_at = record.time.updated_at = "2026-01-01T00:00:00Z"
        return runtime.store.append(record)

    try:
        old_active = append()
        latest = append(status="quarantined")
        # Newer rows visible to a shared-scope list or from a different producer
        # must not become authority for this exact native evidence identity.
        for field in ("tenant_id", "agent_id", "workspace_id", "user_id"):
            append(record_scope=ScopeRef.from_dict({**asdict(scope), field: "other"}))
        append(record_scope=ScopeRef.from_dict({**asdict(scope), "user_id": ""}))
        append(record_source="other.producer")
        append(kind="reflection")
        append(key="other-case")
        # A later rewrite updates an existing row, not its observation order.
        old_active.time.updated_at = "9999-12-31T23:59:59Z"
        runtime.store.rewrite(old_active)
        store = runtime.store if backend == "runtime" else runtime.store.sqlite
        query = {
            "kind": "learning_eval", "source": source,
            "scope": asdict(scope) if backend == "runtime" else scope,
            "meta_key": "observation_key", "meta_value": "case",
        }
        with runtime.store._lock:
            lookup = store.latest_record_by_meta_value_exact_scope
            assert lookup(**query).record_id == old_active.record_id
            assert lookup(**query, status="active").record_id == old_active.record_id
            assert lookup(**query, status=None).record_id == latest.record_id
            assert lookup(**query, status="quarantined").record_id == latest.record_id
            assert lookup(**query, status="missing") is None
    finally:
        runtime.close()
