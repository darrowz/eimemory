"""Tests for the 1.6.0 audit fix:

* ``rollback_capability_candidate`` end-to-end (CLI ``patch rollback`` path)
* ``_enforce_harness_patch_v2`` runs ``enforce_diff_size`` and
  ``enforce_one_active_per_surface`` inside ``promote_candidate``
* ``enforce_diversity`` is called at the end of ``generate_candidate_policies``
* ``HARNESS_PATCH_V2`` runtime env-var reads in ``capability_ledger`` and
  ``promotion_manager`` (after process start, flipping the env var must take
  effect immediately)
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.candidate_search import (
    MIN_DIVERSE_CANDIDATES,
    generate_candidate_policies,
)
from eimemory.governance.harness_patch import (
    HarnessSurface,
    ProposalCard,
    _is_v2_enabled,
)
from eimemory.governance.promotion_manager import (
    _enforce_harness_patch_v2,
    rollback_capability_candidate,
)


PASSING_EVAL = {"verdict": "pass", "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8}}


# ---------------------------------------------------------------------------
# Audit-fix bug 2: CLI ``patch rollback`` now calls rollback_capability_candidate
# ---------------------------------------------------------------------------


def _build_candidate(meta_extra: dict | None = None) -> SimpleNamespace:
    meta = {
        "status": "candidate",
        "authority_tier": "L1",
        "promotion_target": "tool_route",
        "experiment_id": "exp_x",
    }
    if meta_extra:
        meta.update(meta_extra)
    return SimpleNamespace(
        record_id="cap-rollback-001",
        kind="capability_candidate",
        status="candidate",
        summary="roll me back",
        title="Roll back me",
        scope=None,
        content={"capability": "tool.routing", "summary": "roll me back"},
        meta=meta,
    )


class _FakeStore:
    def __init__(self, candidate):
        self._candidate = candidate
        self.writes: list[SimpleNamespace] = []

    def get_by_id(self, _id, scope=None):
        return self._candidate

    def rewrite(self, candidate):
        self._candidate = candidate
        self.writes.append(candidate)


def test_rollback_capability_candidate_flips_status_and_meta() -> None:
    candidate = _build_candidate()
    runtime = SimpleNamespace(store=_FakeStore(candidate))
    result = rollback_capability_candidate(runtime, candidate_id="cap-rollback-001")
    assert result["ok"] is True
    assert result["previous_status"] == "candidate"
    assert result["new_status"] == "rolled_back"
    assert candidate.status == "rolled_back"
    assert candidate.meta["rolled_back_by"] == "eimemory.cli.patch"
    assert runtime.store.writes == [candidate]


def test_rollback_capability_candidate_is_idempotent() -> None:
    candidate = _build_candidate()
    runtime = SimpleNamespace(store=_FakeStore(candidate))
    first = rollback_capability_candidate(runtime, candidate_id="cap-rollback-001")
    assert first.get("already_rolled_back") is None
    second = rollback_capability_candidate(runtime, candidate_id="cap-rollback-001")
    assert second["already_rolled_back"] is True
    # No second write when already rolled back.
    assert len(runtime.store.writes) == 1


def test_rollback_capability_candidate_rejects_non_capability(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    with pytest.raises(ValueError, match="capability candidate not found"):
        rollback_capability_candidate(runtime, candidate_id="missing")


# ---------------------------------------------------------------------------
# Audit-fix bug 1: _enforce_harness_patch_v2 wires enforce_diff_size + one_active
# ---------------------------------------------------------------------------


def _make_runtime_with_card(card: ProposalCard, *, active_surfaces: list[dict] | None = None):
    candidate = SimpleNamespace(
        record_id="cap-promote-001",
        kind="capability_candidate",
        status="candidate",
        summary="promote me",
        title="Promote me",
        scope=None,
        content={
            "capability": "tool.routing",
            "summary": "promote me",
            "proposal_card": {
                "target_surface": card.target_surface.value,
                "evidence_record_ids": list(card.evidence_record_ids),
                "expected_delta": card.expected_delta,
                "target_agent": card.target_agent,
                "risk_tier": card.risk_tier,
                "rollback_plan": card.rollback_plan,
                "diff_lines": card.diff_lines,
                "diff_tokens": card.diff_tokens,
            },
        },
        meta={"status": "candidate", "authority_tier": card.risk_tier},
    )

    class _ActiveSurfaceCandidate:
        def __init__(self, surface: str, rid: str):
            self.record_id = rid
            self.content = {"proposal_card": {"target_surface": surface}}
            self.meta = {"status": "active"}

    class _ListRecords:
        def __call__(self, *, kinds, scope, limit):
            return [_ActiveSurfaceCandidate(d["target_surface"], d["id"]) for d in (active_surfaces or [])]

    class _Store:
        def get_by_id(self, _id, scope=None):
            return candidate

        list_records = _ListRecords()
        rewrite = lambda self, c: None

    return SimpleNamespace(store=_Store()), candidate


def test_enforce_harness_patch_v2_rejects_oversized_diff_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    card = ProposalCard(
        target_surface=HarnessSurface.INSTRUCTION,
        evidence_record_ids=("r1",),
        expected_delta=0.05,
        target_agent="eibrain",
        risk_tier="L1",
        rollback_plan="revert",
        diff_lines=10_000,
        diff_tokens=200,
    )
    runtime, _ = _make_runtime_with_card(card)
    with pytest.raises(ValueError, match="diff_lines"):
        _enforce_harness_patch_v2(runtime, runtime.store.get_by_id("x"), scope=None)


def test_enforce_harness_patch_v2_rejects_duplicate_surface_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    card = ProposalCard(
        target_surface=HarnessSurface.INSTRUCTION,
        evidence_record_ids=("r1",),
        expected_delta=0.05,
        target_agent="eibrain",
        risk_tier="L1",
        rollback_plan="revert",
        diff_lines=10,
        diff_tokens=200,
    )
    runtime, _ = _make_runtime_with_card(card, active_surfaces=[{"target_surface": "INSTRUCTION", "id": "other"}])
    with pytest.raises(ValueError, match="already active"):
        _enforce_harness_patch_v2(runtime, runtime.store.get_by_id("x"), scope=None)


def test_enforce_harness_patch_v2_is_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_PATCH_V2", raising=False)
    card = ProposalCard(
        target_surface=HarnessSurface.INSTRUCTION,
        evidence_record_ids=("r1",),
        expected_delta=0.05,
        target_agent="eibrain",
        risk_tier="L1",
        rollback_plan="revert",
        diff_lines=10_000,
        diff_tokens=999_999,
    )
    runtime, _ = _make_runtime_with_card(card, active_surfaces=[{"target_surface": "INSTRUCTION", "id": "other"}])
    # No raise: v2 is off.
    _enforce_harness_patch_v2(runtime, runtime.store.get_by_id("x"), scope=None)


# ---------------------------------------------------------------------------
# Audit-fix bug 1: enforce_diversity is invoked from generate_candidate_policies
# ---------------------------------------------------------------------------


def _replay_cases_for_source(source: str, *, count: int) -> list[dict]:
    cases: list[dict] = []
    for i in range(count):
        cases.append({
            "task_type": f"tt_{source}",
            "primary_label": "bad_outcome",
            "signals": [source],
            "risk_level": "low",
            "source_outcome_trace_id": f"trace_{source}_{i}",
            "expected_text": ["expected"],
            "query": f"q_{source}_{i}",
        })
    return cases


def test_generate_candidate_policies_enforces_diversity_for_multi_source(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    # Three distinct sources, all repeat-threshold survivors, must enforce.
    cases = (
        _replay_cases_for_source("operator_gap", count=3)
        + _replay_cases_for_source("visual_evidence_gap", count=3)
        + _replay_cases_for_source("world_state_mismatch", count=3)
    )
    candidates = generate_candidate_policies(cases)
    assert len(candidates) >= MIN_DIVERSE_CANDIDATES
    distinct = {c.get("source_key") for c in candidates}
    assert len(distinct) >= MIN_DIVERSE_CANDIDATES


def test_generate_candidate_policies_single_cluster_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    cases = _replay_cases_for_source("operator_gap", count=3)
    candidates = generate_candidate_policies(cases)
    # Single source: leave backwards-compat behavior (no diversity raise).
    assert len(candidates) >= 1


# ---------------------------------------------------------------------------
# Audit-fix bug 3: HARNESS_PATCH_V2 is read at call time
# ---------------------------------------------------------------------------


def test_harness_patch_v2_runtime_helper_reacts_to_setenv(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_PATCH_V2", raising=False)
    assert _is_v2_enabled() is False
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    assert _is_v2_enabled() is True
    monkeypatch.setenv("HARNESS_PATCH_V2", "0")
    assert _is_v2_enabled() is False


def test_capability_ledger_enforces_proposal_card_after_runtime_setenv(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HARNESS_PATCH_V2", raising=False)

    runtime = Runtime.create(root=tmp_path)

    # Setting HARNESS_PATCH_V2=1 in an already-running Python process must
    # take effect immediately (regression test for the module-level constant
    # bug found in the audit).
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")

    from eimemory.governance.capability_ledger import record_capability_score

    with pytest.raises(ValueError, match="proposal_card"):
        record_capability_score(
            runtime,
            scope={"agent_id": "hongtu"},
            loop_id="learn_audit",
            capability="tool.routing",
            score=0.9,
            meta={"authority_tier": "L2", "kind": "candidate_promotion"},
        )


def test_capability_ledger_enforces_invalid_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")

    runtime = Runtime.create(root=tmp_path)
    from eimemory.governance.capability_ledger import record_capability_score

    with pytest.raises(ValueError, match="target_surface invalid"):
        record_capability_score(
            runtime,
            scope={"agent_id": "hongtu"},
            loop_id="learn_audit",
            capability="tool.routing",
            score=0.9,
            meta={
                "authority_tier": "L2",
                "kind": "candidate_promotion",
                "proposal_card": {"target_surface": "BOGUS_SURFACE"},
            },
        )


# ---------------------------------------------------------------------------
# Audit-fix regression gate: evaluate_harness_gate legacy path (v2 unset)
# ---------------------------------------------------------------------------


def test_evaluate_harness_gate_legacy_synthesizes_card(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_PATCH_V2", raising=False)
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_id=lambda _id, scope=None: SimpleNamespace(content={})))
    from eimemory.governance.regression_watch import evaluate_harness_gate
    result = evaluate_harness_gate(
        runtime,
        candidate_id="c1",
        held_in_scores={"accuracy": 0.85},
        held_out_scores=None,
        baseline_held_in=0.80,
        baseline_held_out=None,
    )
    assert result["verdict"] in {"ACCEPT", "WARN", "REJECT"}
