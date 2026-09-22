"""B01/B02: active-surface lease and applied-artifact rollback."""
from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance import promotion_manager as pm
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import (
    _acquire_active_surface_lease,
    _release_active_surface_lease,
    promote_candidate,
    rollback_capability_candidate,
)


PASSING_EVAL = {
    "verdict": "pass",
    "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8},
}


def test_active_surface_lease_is_exclusive(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    first = _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    with pytest.raises(ValueError, match="active_surface_lease_unavailable"):
        _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    _release_active_surface_lease(first)
    second = _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    _release_active_surface_lease(second)


def test_promote_fails_closed_when_surface_lease_busy(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b01",
        experiment_id="exp_b01",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="lease busy",
        target_capability="tool.routing",
    )

    def busy(*_a, **_k):
        raise ValueError("active_surface_lease_unavailable")

    monkeypatch.setattr(pm, "_acquire_active_surface_lease", busy)
    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=False,
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "active_surface_lease_unavailable"


def test_artifact_rollback_undoes_intent_pattern_when_possible(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b02",
        experiment_id="exp_b02",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="pattern rollback",
        target_capability="tool.routing",
    )
    # Apply for real so an intent pattern artifact exists.
    promoted = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=True,
    )
    assert promoted.get("ok") is True, promoted
    assert promoted.get("applied_artifact_ids"), promoted
    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        reason="b02 test",
    )
    assert result["ok"] is True, result
    stored = runtime.store.get_by_id(candidate_id, scope=scope)
    assert stored.status == "rolled_back"
    assert stored.meta.get("applied_artifact_ids") == []


def test_unsupported_code_artifact_rollback_stays_required(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b02b",
        experiment_id="exp_b02b",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="unsupported artifact",
        target_capability="tool.routing",
    )
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    candidate.meta["applied_artifact_ids"] = ["src/eimemory/foo.py"]
    candidate.meta["promotion_target"] = "code_patch"
    candidate.content["promotion_target"] = "code_patch"
    runtime.store.rewrite(candidate)
    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        reason="cannot undo code path",
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "artifact_rollback_required"
    assert "src/eimemory/foo.py" in result.get("unsupported_artifact_kinds", [])
