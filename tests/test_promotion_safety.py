"""L3+ safety wire enforcement in promotion_manager.

Verifies that ``_check_safety_wire`` rejects L3/L4 candidates whose
``safety_wire`` does not declare every required governance module
(kill_switch, circuit_breaker, spend_guard, audit_verifier) and that
tiers below L3 pass through unchanged.
"""
from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion_manager import _check_safety_wire, promote_candidate
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_check_safety_wire_rejects_l3_missing_modules() -> None:
    with pytest.raises(ValueError, match="safety_wire"):
        _check_safety_wire(
            authority_tier="L3",
            safety_wire=("kill_switch",),  # missing the other 3
        )


def test_check_safety_wire_accepts_l3_with_all_four() -> None:
    _check_safety_wire(
        authority_tier="L3",
        safety_wire=("kill_switch", "circuit_breaker", "spend_guard", "audit_verifier"),
    )


def test_check_safety_wire_skips_below_l3() -> None:
    # L2 and below do not require the wire
    _check_safety_wire(authority_tier="L2", safety_wire=())
    _check_safety_wire(authority_tier="L0", safety_wire=())


def test_promote_candidate_enforces_content_authority_tier_safety_wire(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="High authority content-tier candidate",
        summary="Content-only L4 candidates must still pass the safety wire gate.",
        status="candidate",
        scope=ScopeRef.from_dict(scope),
        content={
            "authority_tier": "L4",
            "promotion_target": "tool_route",
            "target_capability": "tool.routing",
            "safety_wire": ["kill_switch"],
        },
        meta={"promotion_target": "tool_route", "target_capability": "tool.routing"},
    )
    runtime.store.append(candidate)

    result = promote_candidate(
        runtime,
        candidate_id=candidate.record_id,
        scope=scope,
        loop_id="safety_wire_content_tier",
        apply=False,
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["blocked_reason"] == "safety_wire_missing"
    promotion = runtime.store.get_by_id(result["promotion_request_id"], scope=scope)
    assert promotion is not None
    assert promotion.status == "blocked"
    assert promotion.content["gate"]["blocked_reasons"] == ["safety_wire_missing"]
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    entry = next(item for item in ledger if item["promotion_id"] == result["promotion_request_id"])
    assert entry["budget_decision"] == "blocked"
    assert entry["reason"] == "safety_wire_missing"


def test_promote_candidate_tolerates_malformed_harness_diff_size(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="Malformed harness diff size candidate",
        summary="Harness diff size should default safely instead of crashing promotion.",
        status="candidate",
        scope=ScopeRef.from_dict(scope),
        content={
            "authority_tier": "L1",
            "promotion_target": "tool_route",
            "target_capability": "tool.routing",
            "proposal_card": {
                "target_surface": "INSTRUCTION",
                "diff_lines": "bad",
                "diff_tokens": "bad",
            },
        },
        meta={"promotion_target": "tool_route", "target_capability": "tool.routing"},
    )
    runtime.store.append(candidate)

    result = promote_candidate(
        runtime,
        candidate_id=candidate.record_id,
        scope=scope,
        loop_id="harness_diff_size",
        apply=False,
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
        health={"ok": True},
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
