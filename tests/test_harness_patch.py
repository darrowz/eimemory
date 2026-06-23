"""Tests for eimemory.governance.harness_patch — Tasks 1 & 2 of the
1.6.0 harness-patch plan: HarnessSurface enum, ProposalCard dataclass,
and HarnessGate dual-regression evaluator.
"""
from __future__ import annotations

import pytest

from eimemory.governance.harness_patch import (
    GateVerdict,
    HarnessGate,
    HarnessSurface,
    ProposalCard,
)


# ---------------------------------------------------------------------------
# Task 1: HarnessSurface enum + ProposalCard dataclass
# ---------------------------------------------------------------------------


def test_harness_surface_has_five_values() -> None:
    assert {s.value for s in HarnessSurface} == {
        "INSTRUCTION",
        "VERIFICATION_GUIDANCE",
        "TOOL_LOOP_GUARD",
        "ARTIFACT_RECOVERY",
        "RUNTIME_POLICY",
    }


def test_proposal_card_requires_all_fields() -> None:
    with pytest.raises(TypeError):
        ProposalCard(  # type: ignore[call-arg]
            target_surface=HarnessSurface.INSTRUCTION,
            # missing evidence_record_ids, expected_delta, target_agent,
            # risk_tier, rollback_plan, diff_lines, diff_tokens
        )


# ---------------------------------------------------------------------------
# Task 2: HarnessGate — dual regression evaluator
# ---------------------------------------------------------------------------


def test_harness_gate_accepts_when_both_splits_up() -> None:
    card = ProposalCard(
        target_surface=HarnessSurface.INSTRUCTION,
        evidence_record_ids=("r1", "r2"),
        expected_delta=0.05,
        target_agent="eibrain",
        risk_tier="L1",
        rollback_plan="revert patch file",
        diff_lines=20,
        diff_tokens=500,
    )
    gate = HarnessGate(card)
    result = gate.evaluate(
        held_in_scores={"accuracy": 0.85, "regression": 0.95},
        held_out_scores={"accuracy": 0.82, "regression": 0.93},
        baseline_held_in=0.80,
        baseline_held_out=0.80,
    )
    assert result.verdict == GateVerdict.ACCEPT
    assert result.delta is not None and result.delta > 0


def test_harness_gate_rejects_when_held_in_drops() -> None:
    card = ProposalCard(
        target_surface=HarnessSurface.RUNTIME_POLICY,
        evidence_record_ids=("r1",),
        expected_delta=0.10,
        target_agent="eibrain",
        risk_tier="L1",
        rollback_plan="revert",
        diff_lines=10,
        diff_tokens=200,
    )
    gate = HarnessGate(card)
    result = gate.evaluate(
        held_in_scores={"accuracy": 0.75},
        held_out_scores={"accuracy": 0.85},
        baseline_held_in=0.80,
        baseline_held_out=0.80,
    )
    assert result.verdict == GateVerdict.REJECT
    assert "held_in" in result.reason


def test_harness_gate_rejects_when_held_out_drops() -> None:
    card = ProposalCard(
        target_surface=HarnessSurface.VERIFICATION_GUIDANCE,
        evidence_record_ids=("r1",),
        expected_delta=0.10,
        target_agent="openclaw",
        risk_tier="L1",
        rollback_plan="revert",
        diff_lines=5,
        diff_tokens=100,
    )
    gate = HarnessGate(card)
    result = gate.evaluate(
        held_in_scores={"accuracy": 0.85},
        held_out_scores={"accuracy": 0.75},
        baseline_held_in=0.80,
        baseline_held_out=0.80,
    )
    assert result.verdict == GateVerdict.REJECT
    assert "held_out" in result.reason


def test_harness_gate_warns_when_split_missing() -> None:
    card = ProposalCard(
        target_surface=HarnessSurface.TOOL_LOOP_GUARD,
        evidence_record_ids=("r1",),
        expected_delta=0.05,
        target_agent="mcp_consumer",
        risk_tier="L0",
        rollback_plan="revert",
        diff_lines=3,
        diff_tokens=80,
    )
    gate = HarnessGate(card)
    result = gate.evaluate(
        held_in_scores={"accuracy": 0.85},
        held_out_scores=None,  # no held-out data yet
        baseline_held_in=0.80,
        baseline_held_out=None,
    )
    assert result.verdict == GateVerdict.WARN
    assert "held_out" in result.reason
