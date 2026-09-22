"""A4: l5 evidence counts distinguish zero-evidence vs unavailable."""
from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.l5_readiness import _evidence_counts_with_health
from eimemory.models.records import RecordEnvelope, ScopeRef

SCOPE = {"agent_id": "hongtu", "tenant_id": "t", "workspace_id": "w", "user_id": "u"}


def test_query_failure_is_evidence_unavailable(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        runtime.store.append(
            RecordEnvelope.create(kind="memory", title="seed", scope=scope_ref)
        )
        monkeypatch.setattr(
            runtime.store,
            "count_records_exact_scope",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sqlite unavailable")),
        )
        counts, health = _evidence_counts_with_health(runtime, scope=scope_ref, limit=100)
    finally:
        runtime.close()
    assert counts["memory"] == 0
    assert health["status"] == "evidence_unavailable"
    assert health["ok"] is False
    assert any(item["kind"] == "memory" for item in health["unavailable_kinds"])


def test_empty_store_is_zero_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        counts, health = _evidence_counts_with_health(runtime, scope=scope_ref, limit=100)
    finally:
        runtime.close()
    assert counts["memory"] == 0
    assert health["status"] == "zero_evidence"
    assert health["ok"] is True
