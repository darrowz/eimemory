"""SECURITY §4 leftovers: digest reconciliation, health identity, scheduler lease reread."""
from __future__ import annotations

from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion_manager import (
    _health_identity_binding_error,
    _reconcile_effect_owner_digests,
    _rollout_gate,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _effects_unknown_after_timeout


PASSING_EVAL = {
    "verdict": "pass",
    "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8},
}


def test_health_identity_requires_commit_version_freshness() -> None:
    assert _health_identity_binding_error(None) == 'health_identity_missing'
    assert _health_identity_binding_error({"ok": True}) == 'health_identity_unbound'
    assert (
        _health_identity_binding_error({"ok": True, "commit": "abc", "version": "1.0"})
        == 'health_freshness_unbound'
    )
    assert (
        _health_identity_binding_error(
            {"ok": True, "commit": "abc", "version": "1.0", "fresh": True}
        )
        == ""
    )


def test_rollout_gate_rejects_unbound_health_for_gated_tier() -> None:
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="gate",
        summary="gate",
        scope=ScopeRef(agent_id="hongtu"),
        source="test",
        content={"authority_tier": "L2", "promotion_target": "eval_case"},
        meta={"authority_tier": "L2", "promotion_target": "eval_case"},
    )
    gate = _rollout_gate(PASSING_EVAL, {"ok": True}, tier="L2", candidate=candidate)
    assert gate["ok"] is False
    assert "health_gate" in gate["blocked_reasons"]
    assert any("health_" in reason for reason in gate["blocked_reasons"])


def test_effect_owner_digest_mismatch_fail_closed(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="digest",
        summary="digest",
        scope=ScopeRef(agent_id="hongtu"),
        source="test",
        content={},
        meta={"applied_artifact_ids": ["missing-artifact"]},
    )
    result = _reconcile_effect_owner_digests(
        runtime,
        candidate=candidate,
        scope={"agent_id": "hongtu"},
        side_effect={
            "applied_artifact_ids": ["missing-artifact"],
            "artifact_digests": {"missing-artifact": "deadbeef"},
            "lifecycle_recorded": True,
        },
        watch={"ok": True},
    )
    assert result["ok"] is False
    assert result.get("requires_reconciliation") is True
    problems = result.get("reconciliation_problems") or []
    assert any("artifact_missing:" in p or "digest" in p for p in problems)


def test_scheduler_timeout_rereads_lease_as_effects_unknown() -> None:
    lease_reads = {"n": 0}

    def reread():
        lease_reads["n"] += 1
        return {"owner": "other", "state": "busy", "side_effects": "unknown"}

    report = _effects_unknown_after_timeout(
        {"ok": True, "timeout_exceeded": True, "applied_count": 1},
        lease_reread=reread(),
    )
    assert report["ok"] is False
    assert report["effects_unknown"] is True
    assert lease_reads["n"] == 1
    assert report.get("blocked_reason")
