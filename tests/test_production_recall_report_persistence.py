"""Report attempt reuse must bind the event, not only returned rankings."""
from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate as gate
from eimemory.models.records import RecallBundle, ScopeRef
from test_production_real_query_gate import (
    BASE_SCOPE, RELEASE, _append_records_and_label_evidence, _case, _dataset,
    _receipt, _record, _refresh_dataset_evidence,
)


@pytest.mark.parametrize("field,value", [
    ("evaluator_commit", "c" * 40),
    ("baseline_capture", True),
    ("accepted", True),
    ("gate_status", "accepted"),
    ("blocked_reason", "pre_switch_bootstrap_anchor"),
    ("baseline_identity", {"persisted_record_id": "prga_different_parent"}),
    ("threshold_gate", {"ok": False, "blocking_metrics": {"baseline": {}}}),
    ("release_identity", {"release_commit": RELEASE.commit, "release_session_id": "other"}),
])
def test_adjacent_equal_rankings_cannot_reuse_different_event_semantics(tmp_path, field, value):
    runtime = Runtime.create(root=tmp_path)
    frozen = gate.freeze_production_recall_dataset(_dataset({
        channel: f"missing-{channel}" for channel in ("openclaw", "codex", "hermes")
    }))
    report = gate._not_run_real_query_report(frozen, "accepted_label_record_missing", release=RELEASE)
    try:
        first = gate._persist_eligible_high_water(
            runtime, report, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
        )
        changed = deepcopy(report)
        changed[field] = value
        second = gate._persist_eligible_high_water(
            runtime, changed, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
        )
        assert second["persisted_record_id"] != first["persisted_record_id"]
        assert second[field] == value
        assert second["previous_attempt_id"] == first["persisted_record_id"]
        repeated = gate._persist_eligible_high_water(
            runtime, changed, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
        )
        assert repeated["persisted_record_id"] == second["persisted_record_id"]
        old = runtime.store.get_by_id(first["persisted_record_id"], scope=BASE_SCOPE)
        assert old.content["report"].get(field) == report.get(field)
    finally:
        runtime.close()


@pytest.fixture
def bootstrap_input(tmp_path, monkeypatch):
    runtime = Runtime.create(root=tmp_path)
    memory = _record("bootstrap-memory", "hermes", "source-hermes")
    _append_records_and_label_evidence(runtime, {"hermes": memory})
    dataset = {"schema": gate.PRODUCTION_REAL_QUERY_SCHEMA, "dataset_kind": "production",
               "scope": dict(BASE_SCOPE), "baseline_report_id": "",
               "cases": [_case("hermes", "source-hermes", memory.record_id, index=i)
                         for i in range(3)]}
    _refresh_dataset_evidence(dataset)
    monkeypatch.setattr(runtime.memory, "recall", lambda **_: RecallBundle(
        items=[memory], rules=[], reflections=[], confidence=1.0, next_action_hint="",
        explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
    ))
    prior = _receipt(runtime, commit="b" * 40, version="1.14.132", prior_commit="a" * 40)
    runtime._test_runtime_commit = prior.commit
    monkeypatch.setattr(gate, "_verified_live_prior_release", lambda *_, **__: (prior, ""))
    try:
        yield runtime, dataset, prior
    finally:
        runtime.close()


def _bootstrap(bootstrap_input, candidate="c" * 40, persist=True):
    runtime, dataset, prior = bootstrap_input
    return gate.bootstrap_production_recall_baseline(
        runtime, dataset, candidate_commit=candidate, prior_commit=prior.commit,
        persist_report=persist,
    )


def test_bootstrap_rejects_a_persistence_result_that_is_an_old_failed_report(bootstrap_input, monkeypatch):
    runtime, dataset, prior = bootstrap_input
    failed = gate._not_run_real_query_report(
        gate.freeze_production_recall_dataset(dataset), "baseline_chain_depth_exceeded", release=prior,
    )
    old = gate._persist_eligible_high_water(
        runtime, failed, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
    )
    monkeypatch.setattr(gate, "_persist_eligible_high_water", lambda *_, **__: deepcopy(old))
    result = _bootstrap(bootstrap_input)
    assert result["bootstrap_status"] == "blocked"
    assert result["ok"] is False
    assert gate._latest_bootstrap_state(runtime, scope=ScopeRef.from_dict(BASE_SCOPE)) is None
    stored = runtime.store.get_by_id(old["persisted_record_id"], scope=BASE_SCOPE)
    assert stored.content["report"]["blocked_reason"] == "baseline_chain_depth_exceeded"


def test_bootstrap_same_candidate_retries_reuse_but_new_candidate_appends(bootstrap_input):
    runtime, _, _ = bootstrap_input
    first = _bootstrap(bootstrap_input)
    assert first["bootstrap_status"] == "anchor_ready"
    assert first["evaluator_commit"] == "c" * 40
    assert _bootstrap(bootstrap_input)["persisted_record_id"] == first["persisted_record_id"]
    next_candidate = _bootstrap(bootstrap_input, candidate="d" * 40)
    assert next_candidate["bootstrap_status"] == "anchor_ready"
    assert next_candidate["evaluator_commit"] == "d" * 40
    assert next_candidate["persisted_record_id"] != first["persisted_record_id"]
    assert next_candidate["previous_attempt_id"] == first["persisted_record_id"]
    old = runtime.store.get_by_id(first["persisted_record_id"], scope=BASE_SCOPE)
    assert old.content["report"]["evaluator_commit"] == "c" * 40


def test_bootstrap_rejects_a_valid_anchor_for_another_candidate(bootstrap_input, monkeypatch):
    old = _bootstrap(bootstrap_input)
    monkeypatch.setattr(gate, "_persist_eligible_high_water", lambda *_, **__: deepcopy(old))
    result = _bootstrap(bootstrap_input, candidate="d" * 40)
    assert result["bootstrap_status"] == "blocked"
    assert result["bootstrap_blocked_reason"] == "bootstrap_anchor_persistence_invalid"
    runtime, _, _ = bootstrap_input
    state = gate._latest_bootstrap_state(runtime, scope=ScopeRef.from_dict(BASE_SCOPE))
    assert state["candidate_commit"] == "c" * 40


def test_bootstrap_rejects_an_anchor_superseded_before_readiness_is_recorded(bootstrap_input, monkeypatch):
    runtime, dataset, prior = bootstrap_input
    original = gate._persist_eligible_high_water

    def concurrent_failed_attempt(*args, **kwargs):
        result = original(*args, **kwargs)
        failed = gate._not_run_real_query_report(
            gate.freeze_production_recall_dataset(dataset), "retrieval_incomplete", release=prior,
        )
        original(runtime, failed, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True)
        return result

    monkeypatch.setattr(gate, "_persist_eligible_high_water", concurrent_failed_attempt)
    result = _bootstrap(bootstrap_input)
    assert result["bootstrap_status"] == "blocked"
    assert result["ok"] is False
    assert gate._latest_bootstrap_state(runtime, scope=ScopeRef.from_dict(BASE_SCOPE)) is None


def test_deterministic_existing_attempt_with_different_semantics_is_a_conflict(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    frozen = gate.freeze_production_recall_dataset(_dataset({
        channel: f"missing-{channel}" for channel in ("openclaw", "codex", "hermes")
    }))
    report = gate._not_run_real_query_report(frozen, "accepted_label_record_missing", release=RELEASE)
    attempt_id = "prga_" + gate._stable_digest({
        "report_id": report["report_id"], "previous_attempt_id": "",
        "attempt_payload_digest": gate._stable_digest(gate._real_query_attempt_payload(report)),
    })[:32]
    forged = deepcopy(report)
    forged["evaluator_commit"] = "d" * 40
    collision = gate._real_query_report_record(
        forged, scope=ScopeRef.from_dict(BASE_SCOPE), attempt_id=attempt_id,
    )
    collision.meta["release_high_water_key"] = "untrusted-high-water-projection"
    try:
        runtime.store.append(collision)
        with pytest.raises(ValueError, match="deterministic production recall attempt conflict"):
            gate._persist_eligible_high_water(
                runtime, report, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
            )
        old = runtime.store.get_by_id(attempt_id, scope=BASE_SCOPE)
        assert old.content["report"]["evaluator_commit"] == "d" * 40
    finally:
        runtime.close()
