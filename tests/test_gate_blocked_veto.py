"""Regression tests for the gate=blocked veto (Task 1.3).

The 6/17 evidence showed the existing promotion pipeline accepted a candidate
whose `eval_suite.gates` evidence was majority `gate=blocked` and still passed
because `run_learning_eval` ignored gate outcomes when computing the verdict.

These tests pin the fix: ``gate_blocked_rate > 0.3`` MUST force ``verdict=fail``
and MUST add ``gate_blocked_rate_exceeded`` to ``blocked_reasons``.

Wired through both the new :func:`compute_verdict` helper AND the public
:func:`run_learning_eval` entry point so the bug cannot return via a different
code path.
"""
from __future__ import annotations

import pytest

from eimemory.governance.learning_eval import (
    GATE_BLOCKED_RATE_VETO_THRESHOLD,
    compute_verdict,
    run_learning_eval,
)
from eimemory.api.runtime import Runtime


# ---------------------------------------------------------------------------
# 6/17 evidence shape (real reproduction fixture)
# ---------------------------------------------------------------------------

# Mirror of what the production eval pipeline emits for a candidate whose
# upstream gates repeatedly trip. Each gate is a dict with at minimum
# ``name`` and ``outcome`` (``ok`` or ``blocked``). The original bug ignored
# these and computed verdict from capability / safety / regression scores
# alone.
SIXTEEN_JUNE_GATE_EVIDENCE = [
    {"name": "recall_quality_gate", "outcome": "blocked", "blocked_reason": "recall_quality_gate_failed"},
    {"name": "replay_gate", "outcome": "blocked", "blocked_reason": "negative_replay_signal"},
    {"name": "safe_action_gate", "outcome": "blocked", "blocked_reason": "destructive_change"},
    {"name": "trusted_gate", "outcome": "blocked", "blocked_reason": "trusted_gate_reject"},
    {"name": "audit_gate", "outcome": "ok", "blocked_reason": ""},
]
# gate_blocked_rate = 4/5 = 0.8, well above the 0.3 veto threshold.

PASSING_SCORES = {
    "capability": 0.9,
    "safety": 1.0,
    "regression": 1.0,
    "cost": 0.8,
    "evidence": 0.9,
    "maintainability": 0.9,
    "confidence": 0.9,
}


# ---------------------------------------------------------------------------
# Direct compute_verdict unit tests
# ---------------------------------------------------------------------------


def _has_veto_reason(blocked_reasons: list[str]) -> bool:
    return any(reason.startswith("gate_blocked_rate_exceeded") for reason in blocked_reasons)


def test_compute_verdict_high_blocked_rate_forces_fail() -> None:
    """The 6/17 evidence reproduction: passing scores but majority blocked gates."""
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": SIXTEEN_JUNE_GATE_EVIDENCE},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "fail", "gate_blocked_rate=0.8 must force verdict=fail even with passing scores"
    assert _has_veto_reason(blocked_reasons), blocked_reasons


def test_compute_verdict_low_blocked_rate_does_not_veto() -> None:
    """1 blocked out of 10 gates (rate=0.1) must NOT veto."""
    gates = [{"name": f"gate_{i}", "outcome": "blocked"} for i in range(1)] + [
        {"name": f"gate_{i}", "outcome": "ok"} for i in range(9)
    ]
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": gates},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "pass"
    assert not _has_veto_reason(blocked_reasons)


def test_compute_verdict_at_threshold_does_not_veto() -> None:
    """Exactly 0.3 blocked (3 of 10) must NOT veto (strict greater-than)."""
    gates = [{"name": f"gate_{i}", "outcome": "blocked"} for i in range(3)] + [
        {"name": f"gate_{i}", "outcome": "ok"} for i in range(7)
    ]
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": gates},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "pass", "boundary case rate=0.3 must NOT veto (strict greater-than)"
    assert not _has_veto_reason(blocked_reasons)


def test_compute_verdict_just_above_threshold_vetoes() -> None:
    """4 blocked out of 10 (rate=0.4) must veto."""
    gates = [{"name": f"gate_{i}", "outcome": "blocked"} for i in range(4)] + [
        {"name": f"gate_{i}", "outcome": "ok"} for i in range(6)
    ]
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": gates},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "fail"
    assert _has_veto_reason(blocked_reasons)


def test_compute_verdict_no_gates_in_suite_does_not_veto() -> None:
    """When eval_suite carries no gate evidence at all, no veto."""
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"scores": PASSING_SCORES},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "pass"
    assert not _has_veto_reason(blocked_reasons)


def test_compute_verdict_empty_gate_list_does_not_veto() -> None:
    """An explicit empty gate list is not veto evidence."""
    verdict, _ = compute_verdict(
        eval_suite={"gates": []},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "pass"


def test_compute_verdict_gate_outcome_string_variants() -> None:
    """Recognize both ``outcome`` and ``status`` shapes, and both blocked synonyms."""
    gates = [
        {"name": "g1", "status": "blocked"},
        {"name": "g2", "status": "ok"},
        {"name": "g3", "outcome": "fail"},
        {"name": "g4", "outcome": "ok"},
        {"name": "g5", "status": "pass"},
    ]
    # Blocked = 2 (g1 + g3) out of 5 = 0.4 → veto
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": gates},
        scores=PASSING_SCORES,
        authority_tier="L1",
    )

    assert verdict == "fail"
    assert _has_veto_reason(blocked_reasons)


def test_compute_verdict_l3_unsafe_candidate_still_fails() -> None:
    """L3 + unsafe must still fail with safety_below_threshold even when gates are clean."""
    scores = dict(PASSING_SCORES, safety=1.0)  # authority_tier branch will force safety=0
    verdict, blocked_reasons = compute_verdict(
        eval_suite={"gates": [{"name": "g1", "outcome": "ok"}]},
        scores=scores,
        authority_tier="L3",
    )

    assert verdict == "fail"
    assert "safety_below_threshold" in blocked_reasons
    assert not _has_veto_reason(blocked_reasons)  # gates are clean; veto must not fire


def test_compute_verdict_exposes_veto_threshold_constant() -> None:
    """Public constant so callers (and tests) can reason about the threshold."""
    assert GATE_BLOCKED_RATE_VETO_THRESHOLD == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Integration tests via the public run_learning_eval entry point
# ---------------------------------------------------------------------------


def test_run_learning_eval_vetoes_6_17_evidence(tmp_path) -> None:
    """End-to-end: passing run_learning_eval with the 6/17 evidence must yield verdict=fail."""
    runtime = Runtime.create(root=tmp_path)

    result = run_learning_eval(
        runtime,
        {
            "candidate_id": "cand_6_17_evidence",
            "authority_tier": "L1",
            "source_record_ids": ["rec_1"],
        },
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        eval_suite={"gates": SIXTEEN_JUNE_GATE_EVIDENCE},
    )

    assert result["ok"] is False
    assert result["verdict"] == "fail"
    assert any(r.startswith("gate_blocked_rate_exceeded") for r in result["blocked_reasons"]), result["blocked_reasons"]


def test_run_learning_eval_passes_when_gates_clean(tmp_path) -> None:
    """Control: with no blocked gates, a normal passing candidate still passes."""
    runtime = Runtime.create(root=tmp_path)

    result = run_learning_eval(
        runtime,
        {
            "candidate_id": "cand_clean",
            "authority_tier": "L1",
            "source_record_ids": ["rec_1"],
        },
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        eval_suite={"gates": [{"name": "g1", "outcome": "ok"}]},
    )

    assert result["ok"] is True
    assert result["verdict"] == "pass"
    assert not any(r.startswith("gate_blocked_rate_exceeded") for r in result["blocked_reasons"])
