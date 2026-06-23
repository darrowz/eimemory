"""Tests for ``regression_watch.evaluate_harness_gate`` — Task 5 of the
1.6.0 harness-patch plan.

The wrapper enforces the dual-regression gate
(``held-in ∩ held-out ∩ delta>=0``) and requires a ``proposal_card``
on the candidate when ``HARNESS_PATCH_V2=1``.
"""
from __future__ import annotations

from types import SimpleNamespace

from eimemory.governance.regression_watch import evaluate_harness_gate


def _make_runtime(content: dict) -> SimpleNamespace:
    """Build a minimal runtime stub exposing ``store.get_by_id(_id, scope=...)``."""

    def _get_by_id(_id, scope=None):  # noqa: ARG001
        return SimpleNamespace(content=content)

    return SimpleNamespace(store=SimpleNamespace(get_by_id=_get_by_id))


_CARD_CONTENT = {
    "proposal_card": {
        "target_surface": "INSTRUCTION",
        "evidence_record_ids": ["r1"],
        "expected_delta": 0.05,
        "target_agent": "eibrain",
        "risk_tier": "L1",
        "rollback_plan": "revert",
        "diff_lines": 10,
        "diff_tokens": 200,
    }
}


def test_evaluate_harness_gate_accepts_when_both_up(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    runtime = _make_runtime(_CARD_CONTENT)
    result = evaluate_harness_gate(
        runtime,
        candidate_id="c1",
        held_in_scores={"accuracy": 0.85},
        held_out_scores={"accuracy": 0.82},
        baseline_held_in=0.80,
        baseline_held_out=0.80,
    )
    assert result["verdict"] == "ACCEPT"


def test_evaluate_harness_gate_rejects_missing_card(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    runtime = _make_runtime({})
    result = evaluate_harness_gate(
        runtime,
        candidate_id="c2",
        held_in_scores={"accuracy": 0.85},
        held_out_scores={"accuracy": 0.85},
        baseline_held_in=0.80,
        baseline_held_out=0.80,
    )
    assert result["verdict"] == "REJECT"