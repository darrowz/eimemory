"""Ranking labels must never change the sequence being evaluated."""
from types import SimpleNamespace

import pytest

from eimemory.evaluation import real_query_gate as gate
from eimemory.models.records import RecordEnvelope, ScopeRef


def record(name, *, kind="memory", clone=False):
    value = RecordEnvelope.create(
        kind=kind, title="Ground truth rule" if clone else name,
        summary="plan then replay" if clone else name, scope=ScopeRef(user_id="owner"),
        source="test.delivery", source_id="private",
        content=({"report_type": "ground_truth_behavior_rule", "priority": "T0",
                  "must_use": True, "target_capability": "replay", "expected_behavior": "plan then replay"}
                 if clone else {}),
        meta=({"report_type": "ground_truth_behavior_rule", "priority": "T0",
               "must_use": True, "target_capability": "replay"} if clone else {}),
    )
    value.record_id = name
    return value


def test_changing_labels_cannot_change_the_evaluated_delivery_order():
    items = [record("mem_noise"), record("mem_answer")]
    rules = [record("rule_noise", kind="rule"), record("rule_answer", kind="rule")]
    bundle = SimpleNamespace(items=items, rules=rules, reflections=[])
    first = gate._candidate_records_for_case(bundle, label_kinds={"memory"},
        label_record_ids={items[1].record_id})
    second = gate._candidate_records_for_case(bundle, label_kinds={"rule"},
        label_record_ids={rules[1].record_id})
    assert first == second == items
    assert bundle.items == items and bundle.rules == rules


def test_rule_channel_retains_its_native_order_and_all_delivered_slots():
    rules = [record(f"rule_{index}", kind="rule") for index in range(6)]
    bundle = SimpleNamespace(items=[record("mem_noise")], rules=rules, reflections=[])
    assert gate._candidate_records_for_case(bundle, evaluation_channel="rules",
        label_kinds={"rule"}, label_record_ids={rules[-1].record_id}) == rules


def test_semantic_dedupe_cannot_replace_the_first_delivery_with_labeled_clone():
    first = record("rule_first", kind="rule", clone=True)
    later = record("rule_labeled", kind="rule", clone=True)
    assert gate._record_ranking_ref(first) == gate._record_ranking_ref(later)
    assert gate._dedupe_records_by_ranking_identity([first, later],
        prefer_ids={later.record_id}) == [first]


def test_rank_metrics_keep_duplicate_delivery_slots_and_do_not_refill_top_five():
    metric = gate.evaluate_labeled_ranking_at_5(
        candidate_refs=["noise", "noise", "answer", "tail", "tail", "outside"],
        labels=[{"record_ref": "answer", "grade": 3}, {"record_ref": "outside", "grade": 3}],
        corpus_result_capacity=100,
    )
    assert metric["mrr"] == pytest.approx(1 / 3)
    assert metric["recall_at_5"] == .5
    assert metric["precision_at_5"] == .2


@pytest.fixture
def gate_case(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    from test_production_real_query_gate import BASE_SCOPE, RELEASE, _case, _record, _rule_record, _refresh_dataset_evidence, _trusted_baseline

    runtime = Runtime.create(root=tmp_path)
    memory = _record("mem_answer", "hermes", "source-hermes")
    rule = _rule_record("rule_answer", "hermes", "source-hermes")
    runtime.store.append(memory)
    runtime.store.append(rule)
    dataset = {"schema": gate.PRODUCTION_REAL_QUERY_SCHEMA, "dataset_kind": "production",
        "scope": BASE_SCOPE, "cases": [_case("hermes", "source-hermes", rule.record_id, index=index)
                                       for index in range(3)]}
    monkeypatch.setattr(gate, "_production_proactive_metrics", lambda *_: {})

    def evaluate(bundle, *, channel=None, legacy_baseline=False, label=rule):
        for case in dataset["cases"]:
            case["labels"][0]["record_ref"] = label.record_id
            if channel is None:
                case.pop("evaluation_channel", None)
            else:
                case["evaluation_channel"] = channel
        _refresh_dataset_evidence(dataset)
        frozen = gate.freeze_production_recall_dataset(dataset)
        baseline = _trusted_baseline(dataset)
        baseline["metrics"]["precision_at_5"] = .2
        if legacy_baseline:
            baseline.pop("scoring_contract")
        evaluate.last_baseline = baseline
        monkeypatch.setattr(runtime.memory, "recall", lambda **_: bundle)
        report = gate._evaluate_real_query_candidate(runtime, frozen=frozen, release=RELEASE,
            baseline=baseline, capacities={case["case_id"]: 5 for case in frozen["cases"]})
        return report, frozen

    try:
        yield runtime, memory, rule, evaluate
    finally:
        runtime.close()


def test_gate_reports_rule_lane_without_label_switching_the_primary_channel(gate_case):
    _, memory, rule, evaluate = gate_case
    noise = record("rule_noise", kind="rule")
    bundle = SimpleNamespace(items=[memory], rules=[noise, rule], reflections=[], explanation={})
    report, default_frozen = evaluate(bundle)
    sample = report["samples"][0]
    assert sample["returned_refs"] == [memory.record_id]
    assert sample["recall_at_5"] == 0
    assert sample["evaluation_channel"] == "items"
    assert sample["evaluation_channel_notice"] == "rule_labels_require_explicit_channel_review"
    assert sample["channel_rankings"]["rules"]["returned_refs"] == [noise.record_id, rule.record_id]
    assert sample["channel_rankings"]["rules"]["mrr"] == .5
    declared, rule_frozen = evaluate(bundle, channel="rules")
    assert rule_frozen["dataset_digest"] != default_frozen["dataset_digest"]
    sample = declared["samples"][0]
    assert sample["returned_refs"] == [noise.record_id, rule.record_id]
    assert sample["mrr"] == .5 and sample["evaluation_channel_declared"] is True
    assert declared["scoring_contract"] == gate.DELIVERY_SCORING_CONTRACT


def test_gate_never_pulls_deep_answer_forward_after_semantic_clone_dedupe(gate_case):
    _, memory, _, evaluate = gate_case
    clones = [record(f"rule_clone_{index}", kind="rule", clone=True) for index in range(40)]
    bundle = SimpleNamespace(items=[*clones, memory], rules=[], reflections=[], explanation={})
    report, _ = evaluate(bundle, label=memory)
    sample = report["samples"][0]
    assert sample["returned_refs"] == [item.record_id for item in clones[:5]]
    assert len(sample["returned_ranking_refs"]) == 5
    assert sample["mrr"] == sample["recall_at_5"] == 0
    stored = gate._real_query_report_record(report, scope=memory.scope)
    assert stored.content["report"]["scoring_contract"] == gate.DELIVERY_SCORING_CONTRACT
    assert stored.content["report"]["samples"][0]["returned_refs"] == sample["returned_refs"]


def test_legacy_baseline_cannot_certify_improvement_under_new_scoring(gate_case):
    _, memory, _, evaluate = gate_case
    bundle = SimpleNamespace(items=[memory], rules=[], reflections=[], explanation={})
    report, _ = evaluate(bundle, label=memory, legacy_baseline=True)
    assert report["accepted"] is False
    assert report["blocked_reason"] == "baseline_scoring_contract_mismatch"
    assert report["baseline_comparison"] == {"status": "not_comparable", "reason": "scoring_contract_mismatch"}
    assert all(sample["baseline_ranking_refs"] == [] for sample in report["samples"])


def test_unknown_evaluation_channel_blocks_dataset_before_scoring(gate_case):
    _, memory, _, evaluate = gate_case
    # Validate the public freeze boundary directly; invalid channels do not
    # reach the evaluator's declared-channel selection.
    from test_production_real_query_gate import BASE_SCOPE, _case, _refresh_dataset_evidence
    dataset = {"schema": gate.PRODUCTION_REAL_QUERY_SCHEMA, "dataset_kind": "production",
        "scope": BASE_SCOPE, "cases": [{**_case("hermes", "source-hermes", memory.record_id, index=index),
                                        "evaluation_channel": "label_selected"} for index in range(3)]}
    _refresh_dataset_evidence(dataset)
    frozen = gate.freeze_production_recall_dataset(dataset)
    assert frozen["eligibility"]["ok"] is False
    assert "evaluation_channel_invalid" in frozen["eligibility"]["blocked_reasons"]


@pytest.mark.parametrize("mutation", ["sample_channel", "plan_channel", "plan_and_sample", "rehash_plan", "invocation_depth"])
def test_persisted_channel_plan_detects_mutation_and_binds_baseline(gate_case, mutation):
    from copy import deepcopy
    from test_production_real_query_gate import BASE_SCOPE, RELEASE

    runtime, memory, _, evaluate = gate_case
    bundle = SimpleNamespace(items=[memory], rules=[], reflections=[], explanation={})
    report, frozen = evaluate(bundle, label=memory)
    assert report["accepted"] is True
    assert report["evaluation_plan"]["dataset_digest"] == frozen["dataset_digest"]
    assert report["evaluation_plan"]["invocation_depth"] == 64
    assert report["evaluation_plan"]["scored_depth"] == 5
    assert gate._independent_real_query_metrics_valid(report, baseline=evaluate.last_baseline,
        runtime=runtime, scope=BASE_SCOPE)
    assert gate._validate_persisted_real_query_report(report, expected_release=RELEASE)
    changed = deepcopy(report)
    if mutation in {"sample_channel", "plan_and_sample", "rehash_plan"}:
        changed["samples"][0]["evaluation_channel"] = "rules"
    if mutation in {"plan_channel", "plan_and_sample", "rehash_plan"}:
        changed["evaluation_plan"]["cases"][0]["evaluation_channel"] = "rules"
    if mutation == "invocation_depth":
        changed["evaluation_plan"]["invocation_depth"] = 5
    if mutation in {"rehash_plan", "invocation_depth"}:
        changed["evaluation_plan_digest"] = gate._stable_digest(changed["evaluation_plan"])
    assert not gate._independent_real_query_metrics_valid(changed, baseline=evaluate.last_baseline,
        runtime=runtime, scope=BASE_SCOPE)
    if mutation != "rehash_plan":
        assert not gate._validate_persisted_real_query_report(changed, expected_release=RELEASE)
