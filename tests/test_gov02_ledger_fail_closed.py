"""GOV-02: ledger write failure must block promotion state rewrite."""
from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance import promotion_manager as pm
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate, rollback_capability_candidate


PASSING_EVAL = {
    "verdict": "pass",
    "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8},
}


def test_promote_aborts_when_lifecycle_ledger_unavailable(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="gov02",
        experiment_id="exp_gov02",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="memory-first routing",
        target_capability="tool.routing",
    )

    def boom(*args, **kwargs):
        return {"ok": False, "error": "rollout_ledger_unavailable"}

    monkeypatch.setattr(pm, "_record_candidate_lifecycle", boom)
    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="gov02",
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=True,
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "ledger_record_failed"
    stored = runtime.store.get_by_id(candidate_id, scope=scope)
    assert stored is not None
    assert stored.status == "candidate"


def test_rollback_aborts_when_lifecycle_ledger_unavailable(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="gov02",
        experiment_id="exp_gov02_rb",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="rollback candidate",
        target_capability="tool.routing",
    )

    def boom(*args, **kwargs):
        return {"ok": False, "error": "rollout_ledger_unavailable"}

    monkeypatch.setattr(pm, "_record_candidate_lifecycle", boom)
    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        reason="test",
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "ledger_record_failed"
    stored = runtime.store.get_by_id(candidate_id, scope=scope)
    assert stored is not None
    assert stored.status == "candidate"
