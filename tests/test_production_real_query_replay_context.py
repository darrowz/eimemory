"""Natural replay must retain the captured request's semantic boundaries."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import tracemalloc
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate as gate
from eimemory.evaluation.query_input_vault import capture_query_input
from eimemory.models.records import RecallBundle, ScopeRef
from eimemory.retrieval.query_identity import effective_query_digest, query_text_digest
from test_production_real_query_gate import (
    BASE_SCOPE, RELEASE, _case, _record, _refresh_dataset_evidence, _trusted_baseline,
)


@pytest.fixture
def captured_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_CAPTURE_ORIGINAL_QUERY", "1")
    monkeypatch.delenv("EIMEMORY_CAPTURE_QUERY_SCOPES", raising=False)
    runtime = Runtime.create(root=tmp_path, profile="core")
    label = _record("mem_replay_answer", "hermes", "hermes")
    runtime.store.append(label)
    cases, originals, contexts = [], [], []
    try:
        for index in range(3):
            query = f"请继续完成部署验收再执行nightly复测步骤{index}"
            effective = query + "\nContext entities: genuine prior deployment receipt conversation"
            context = {"exact_scope_only": True, "source_ids": ["hermes"],
                       "runtime_channel": "proactive", "task_type": "research.task"}
            decision = "pd:" + sha256(f"captured-replay-{index}".encode()).hexdigest()[:32]
            runtime.store.record_proactive_decision({
                "decision_id": decision, "channel": "hermes", "scope": asdict(label.scope),
                "source_key": sha256(b"hermes").hexdigest(), "source_ids": ["hermes"],
                "session_id": f"session-{index}", "turn_id": f"turn-{index}",
                "query_id": f"query-{index}", "query_digest": query_text_digest(query),
                "effective_query_digest": effective_query_digest("research.task", effective),
                "task_type": "research.task", "policy_version": "test.proactive.v1",
                "release_identity": {"release_commit": "a" * 40, "release_version": "1.0",
                    "deployment_receipt_id": "receipt", "release_session_id": "release"},
                "release_bound": True, "control_cohort": False, "pair_id": f"pair-{index}",
            }, [], [])
            captured = capture_query_input(runtime, decision_id=decision, query=query,
                effective_query=effective, host_query=query,
                explanation={"task_context": context, "retrieval_status": "evidence_found"})
            assert captured["status"] == "captured"
            case = _case("hermes", "hermes", label.record_id, index=index)
            case["case_id"] = "real-" + gate._stable_digest({
                "decision_id": decision, "query_digest": query_text_digest(query)})[:24]
            case["provenance"]["capture_ref"] = decision
            # This redacted annotation must not become the online request intent.
            case["query_features"] = {"terms": ["receipt", "acceptance", "nightly"],
                                      "intent": "operator_preference"}
            case["query_digest"] = gate._stable_digest(case["query_features"])
            cases.append(case)
            originals.append(effective)
            contexts.append(context)
        dataset = {"schema": gate.PRODUCTION_REAL_QUERY_SCHEMA, "dataset_kind": "production",
                   "scope": BASE_SCOPE, "cases": cases}
        _refresh_dataset_evidence(dataset)
        monkeypatch.setattr(gate, "_production_proactive_metrics", lambda *_: {})
        yield runtime, label, dataset, originals, contexts
    finally:
        runtime.close()


def test_verified_input_retains_semantics_without_feature_intent(captured_cases):
    runtime, _, dataset, originals, _ = captured_cases
    for case, original in zip(dataset["cases"], originals):
        query, context, binding = gate._real_query_recall_inputs(runtime, case)
        assert query == original
        assert context["exact_scope_only"] is True
        assert context["task_type"] == "research.task"
        assert context["runtime_channel"] == "proactive"
        assert "intent" not in context
        assert context["source_ids"] == ["hermes"] and context["target_source_id"] == "hermes"
        assert context["recall_profile"] == "precision" and context["candidate_limit"] == 64
        assert context["evaluation_policy"] == gate.PRODUCTION_REAL_QUERY_POLICY
        assert gate._replay_input_binding_valid(binding)
        assert binding["context_digest"] == gate._stable_digest(context)
        assert query not in json.dumps(binding, ensure_ascii=False)


def test_fixed_gate_controls_cannot_be_changed_by_captured_context(captured_cases, monkeypatch):
    runtime, _, dataset, _, _ = captured_cases
    case = dataset["cases"][0]
    payload = gate._vault_bound_replay_input(runtime, case)
    payload["task_context"].update(source_ids=["foreign"], target_source_id="foreign",
                                  recall_profile="exploratory", candidate_limit=999,
                                  intent="captured real intent", _recall_deadline_monotonic=1)
    monkeypatch.setattr(gate, "_vault_bound_replay_input", lambda *_: payload)
    _, context, _ = gate._real_query_recall_inputs(runtime, case)
    assert context["source_ids"] == ["hermes"] and context["target_source_id"] == "hermes"
    assert context["recall_profile"] == "precision" and context["candidate_limit"] == 64
    assert context["intent"] == "captured real intent"
    assert "_recall_deadline_monotonic" not in context


def test_legacy_features_fallback_is_unchanged(captured_cases, monkeypatch):
    runtime, _, dataset, _, _ = captured_cases
    monkeypatch.setattr(gate, "_vault_bound_replay_input", lambda *_: None)
    query, context, binding = gate._real_query_recall_inputs(runtime, dataset["cases"][0])
    assert query == "receipt acceptance nightly operator_preference"
    assert context["runtime_channel"] == "hermes" and context["intent"] == "operator_preference"
    assert "task_type" not in context and "exact_scope_only" not in context
    assert binding == {"mode": "redacted_features"}


def test_verified_host_payload_corruption_keeps_existing_fallback(captured_cases):
    runtime, _, dataset, _, _ = captured_cases
    case = dataset["cases"][0]
    with runtime.store.locked() as db:
        decision = case["provenance"]["capture_ref"]
        payload = json.loads(db.execute("SELECT payload FROM proactive_query_input_vault WHERE decision_id=?",
                                        (decision,)).fetchone()[0])
        payload["host_query"] = "a different unbound host request"
        db.execute("UPDATE proactive_query_input_vault SET payload=? WHERE decision_id=?",
                   (json.dumps(payload), decision))
        db.commit()
    assert gate._vault_bound_replay_query(runtime, case) == ""
    _, _, binding = gate._real_query_recall_inputs(runtime, case)
    assert binding == {"mode": "redacted_features"}


def _evaluate(captured_cases, monkeypatch, *, empty=False, baseline=None):
    runtime, label, dataset, originals, _ = captured_cases
    calls = []
    def recall(**kwargs):
        calls.append(deepcopy(kwargs))
        assert kwargs["query"] in originals and kwargs["limit"] == 64
        assert kwargs["task_context"]["exact_scope_only"] is True
        assert kwargs["task_context"]["task_type"] == "research.task"
        assert kwargs["task_context"]["runtime_channel"] == "proactive"
        assert "intent" not in kwargs["task_context"]
        return RecallBundle(items=[] if empty else [label], rules=[], reflections=[],
                            confidence=0, next_action_hint="", explanation={})
    monkeypatch.setattr(runtime.memory, "recall", recall)
    frozen = gate.freeze_production_recall_dataset(dataset)
    report = gate._evaluate_real_query_candidate(runtime, frozen=frozen, release=RELEASE,
        baseline=baseline, capacities={case["case_id"]: 5 for case in frozen["cases"]})
    return report, calls, frozen


def test_natural_gate_pins_original_context_for_warmup_and_top5_scoring(captured_cases, monkeypatch):
    reader = gate._vault_bound_replay_input
    loads = []
    def read(runtime, case):
        loads.append(case["case_id"])
        return reader(runtime, case)
    monkeypatch.setattr(gate, "_vault_bound_replay_input", read)
    report, calls, _ = _evaluate(captured_cases, monkeypatch)
    assert len(loads) == 3 and len(calls) == 6
    assert report["metrics"]["recall_at_5"] == 1
    assert report["metrics"]["precision_at_5"] == .2
    assert report["evaluation_plan"]["schema"] == "recall_bundle_delivery_plan.v2"
    assert gate._delivery_evaluation_plan_valid(report)
    for planned, sample in zip(report["evaluation_plan"]["cases"], report["samples"]):
        assert planned["replay_input_binding"] == sample["replay_input_binding"]
        assert sample["replay_input_binding"]["mode"] == "verified_vault"
    assert all(query not in json.dumps(report, ensure_ascii=False) for query in captured_cases[3])


def test_valid_original_context_does_not_excuse_low_recall(captured_cases, monkeypatch):
    report, _, _ = _evaluate(captured_cases, monkeypatch, empty=True)
    assert report["accepted"] is False and report["threshold_gate"]["ok"] is False
    assert {"recall_at_5", "precision_at_5", "mrr", "ndcg_at_5"} <= set(
        report["threshold_gate"]["blocking_metrics"])


def test_warmup_cannot_mutate_the_pinned_nested_context(captured_cases, monkeypatch):
    runtime, label, dataset, _, _ = captured_cases
    calls = []
    def recall(**kwargs):
        calls.append(deepcopy(kwargs["task_context"]))
        assert kwargs["task_context"]["source_ids"] == ["hermes"]
        kwargs["task_context"]["source_ids"].append("mutated-by-provider")
        return RecallBundle(items=[label], rules=[], reflections=[], confidence=0,
                            next_action_hint="", explanation={})
    monkeypatch.setattr(runtime.memory, "recall", recall)
    frozen = gate.freeze_production_recall_dataset(dataset)
    report = gate._evaluate_real_query_candidate(runtime, frozen=frozen, release=RELEASE,
        baseline=None, capacities={case["case_id"]: 5 for case in frozen["cases"]})
    assert len(calls) == 6 and all(context["source_ids"] == ["hermes"] for context in calls)
    assert gate._delivery_evaluation_plan_valid(report)


def test_input_preparation_allocations_obey_the_existing_memory_ceiling(captured_cases, monkeypatch):
    reader = gate._real_query_recall_inputs
    allocated_size = int(gate.PRODUCTION_REAL_QUERY_THRESHOLDS["peak_memory_bytes"]) + 1024 * 1024
    retained = []
    def prepare(runtime, case):
        # Stand in for private inputs retained by the pin, not recall allocations.
        if not retained:
            retained.append(bytearray(allocated_size))
        return reader(runtime, case)
    monkeypatch.setattr(gate, "_real_query_recall_inputs", prepare)
    try:
        report, _, _ = _evaluate(captured_cases, monkeypatch)
        assert report["metrics"]["peak_memory_bytes"] >= allocated_size
        assert "peak_memory_bytes" in report["threshold_gate"]["blocking_metrics"]
        assert report["accepted"] is False
    finally:
        retained.clear()
    assert not tracemalloc.is_tracing()


@pytest.mark.parametrize("stage", ["input", "plan"])
@pytest.mark.parametrize("external", [False, True])
def test_preparation_failure_cleans_up_only_its_own_tracer(captured_cases, monkeypatch, stage, external):
    runtime, _, dataset, _, _ = captured_cases
    def fail(*_, **__):
        raise ValueError("test_input_preparation_failed")
    monkeypatch.setattr(gate, "_real_query_recall_inputs" if stage == "input"
                        else "_delivery_evaluation_plan", fail)
    frozen = gate.freeze_production_recall_dataset(dataset)
    assert not tracemalloc.is_tracing()
    if external:
        tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="test_input_preparation_failed"):
            gate._evaluate_real_query_candidate(runtime, frozen=frozen, release=RELEASE,
                baseline=None, capacities={case["case_id"]: 5 for case in frozen["cases"]})
        assert tracemalloc.is_tracing() is external
    finally:
        if external:
            tracemalloc.stop()


def test_old_plan_remains_readable_but_cannot_be_a_new_plan_baseline(captured_cases, monkeypatch):
    dataset = captured_cases[2]
    frozen = gate.freeze_production_recall_dataset(dataset)
    old_plan = {"schema": "recall_bundle_delivery_plan.v1", "dataset_digest": frozen["dataset_digest"],
        "invocation_depth": 64, "scored_depth": 5,
        "cases": [{"case_id": case["case_id"], "evaluation_channel": "items"}
                  for case in frozen["cases"]]}
    old = {"dataset_digest": frozen["dataset_digest"], "samples": deepcopy(old_plan["cases"]),
           "evaluation_plan": old_plan, "evaluation_plan_digest": gate._stable_digest(old_plan)}
    assert gate._delivery_evaluation_plan_valid(old)
    baseline = _trusted_baseline(dataset)
    baseline["evaluation_plan_digest"] = old["evaluation_plan_digest"]
    report, _, _ = _evaluate(captured_cases, monkeypatch, baseline=baseline)
    assert report["accepted"] is False
    assert report["evaluation_plan_digest"] != old["evaluation_plan_digest"]
    assert report["baseline_comparison"]["reason"] == "evaluation_plan_mismatch"
    assert "baseline" in report["threshold_gate"]["blocking_metrics"]


@pytest.mark.parametrize("field", ["input_digest", "context_digest", "identity_schema", "mode"])
def test_replay_binding_cannot_be_tampered_without_invalidating_plan(captured_cases, monkeypatch, field):
    report, _, _ = _evaluate(captured_cases, monkeypatch)
    report["samples"][0]["replay_input_binding"][field] = "unbound"
    report["evaluation_plan_digest"] = gate._stable_digest(report["evaluation_plan"])
    assert not gate._delivery_evaluation_plan_valid(report)


def test_bootstrap_anchor_different_plan_is_not_reused(captured_cases, monkeypatch):
    runtime, _, dataset, _, _ = captured_cases
    frozen = gate.freeze_production_recall_dataset(dataset)
    scope = ScopeRef.from_dict(BASE_SCOPE)
    high_water = gate._baseline_high_water_key(RELEASE.commit, frozen["dataset_digest"])
    record = SimpleNamespace(record_id="existing-anchor", kind="reflection",
        source="eimemory.evaluation.production_recall", source_id="default", status="active", scope=scope,
        meta={"baseline_high_water_key": high_water}, content={"report": {
            "attempt_id": "existing-anchor", "scope": BASE_SCOPE, "dataset_digest": frozen["dataset_digest"],
            "evaluation_plan_digest": "a" * 64, "evaluator_commit": "b" * 40,
            "baseline_capture": True, "accepted": False, "gate_status": "not_run",
            "blocked_reason": "pre_switch_bootstrap_anchor",
            "threshold_gate": {"ok": False, "blocking_metrics": {"baseline": {}}}}})
    monkeypatch.setattr(runtime.store, "latest_record_by_meta_value_exact_scope", lambda **_: record)
    monkeypatch.setattr(gate, "_record_payload_digest_valid", lambda *_: True)
    monkeypatch.setattr(gate, "_validate_persisted_real_query_report", lambda *_, **__: True)
    monkeypatch.setattr(gate, "_independent_real_query_metrics_valid", lambda *_, **__: True)
    assert gate._qualified_bootstrap_anchor(runtime, scope=scope, release=RELEASE,
        candidate_commit="b" * 40, dataset_digest=frozen["dataset_digest"],
        expected_evaluation_plan_digest="a" * 64) is record
    assert gate._qualified_bootstrap_anchor(runtime, scope=scope, release=RELEASE,
        candidate_commit="b" * 40, dataset_digest=frozen["dataset_digest"],
        expected_evaluation_plan_digest="b" * 64) is None
