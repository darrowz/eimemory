"""Single-positive smoke must not masquerade as judged relevance quality."""
import pytest

from eimemory.evaluation.production_recall import (
    RECALL_QUALITY_GATE_THRESHOLDS,
    evaluate_production_recall_quality_gate,
)


@pytest.mark.parametrize("sample_count", [5, 10])
def test_known_item_smoke_is_insufficient_even_with_perfect_lookup(sample_count):
    report = {
        **RECALL_QUALITY_GATE_THRESHOLDS,
        "sample_count": sample_count,
        "evaluation_contract": "known_item_smoke.v1",
        "hit_at_1": 1.0,
        "hit_at_5": 1.0,
        "mrr": 1.0,
        "p_at_3": 0.333,
        "noise_rate": 0.8,
        "false_recall_rate": 0.0,
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
    }
    gate = evaluate_production_recall_quality_gate(report)
    assert gate["ok"] is False
    assert gate["blocked_reason"] == "recall_quality_evidence_incomplete"
    assert gate["evidence_status"] == "insufficient"
    assert gate["unassessed_metrics"] == ["hit_at_1", "hit_at_5", "mrr", "p_at_3", "noise_rate"]
    assert gate["blocking_metrics"] == {}
    assert gate["thresholds"] == RECALL_QUALITY_GATE_THRESHOLDS
    # The historical numeric diagnostics must not be rewritten into successes.
    assert report["p_at_3"] == 0.333
    assert report["noise_rate"] == 0.8


@pytest.mark.parametrize("sample_count", [5, 10])
@pytest.mark.parametrize("metric,actual", [
    ("false_recall_rate", 0.1),
    ("cross_channel_leakage_count", 1),
    ("source_filter_leakage_count", 1),
    ("payload_bytes_top_1", 4097),
])
def test_known_item_contract_preserves_real_failures(sample_count, metric, actual):
    report = {
        **RECALL_QUALITY_GATE_THRESHOLDS,
        "evaluation_contract": "known_item_smoke.v1",
        "sample_count": sample_count,
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
        metric: actual,
    }
    gate = evaluate_production_recall_quality_gate(report)
    assert gate["ok"] is False
    assert gate["blocked_reason"] == "recall_quality_gate_failed"
    assert gate["blocking_metrics"][metric]["actual"] == actual
    assert gate["evidence_status"] == "insufficient"


def test_known_item_rank_miss_stays_uncertified() -> None:
    report = {
        **RECALL_QUALITY_GATE_THRESHOLDS,
        "sample_count": 10,
        "evaluation_contract": "known_item_smoke.v1",
        "hit_at_1": 0.6,
        "hit_at_5": 0.7,
        "mrr": 0.65,
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
    }
    gate = evaluate_production_recall_quality_gate(report)
    assert gate["ok"] is False
    assert gate["blocked_reason"] == "recall_quality_evidence_incomplete"
    assert gate["blocking_metrics"] == {}


def test_judged_relevance_requires_positive_rewrite_and_no_answer():
    perfect = {
        **RECALL_QUALITY_GATE_THRESHOLDS,
        "sample_count": 12,
        "evaluation_contract": "judged_relevance.v1",
        "hit_at_1": 1.0,
        "hit_at_5": 1.0,
        "mrr": 1.0,
        "p_at_3": 1.0,
        "noise_rate": 0.0,
        "false_recall_rate": 0.0,
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
        "label_trust": "operator_judged",
        "label_roles": ["positive"],
    }
    incomplete = evaluate_production_recall_quality_gate(perfect)
    assert incomplete["ok"] is False
    assert incomplete["blocked_reason"] == "recall_quality_evidence_incomplete"
    covered = {
        **perfect,
        "label_roles": ["no_answer", "positive", "rewrite"],
    }
    ready = evaluate_production_recall_quality_gate(covered)
    assert ready["ok"] is True
    assert ready["blocked_reason"] == ""
    machine = {**covered, "label_trust": "machine_judged"}
    machine_ready = evaluate_production_recall_quality_gate(machine)
    assert machine_ready["ok"] is True


def test_unavailable_store_keeps_known_item_contract():
    from types import SimpleNamespace
    from eimemory.scheduler.jobs import _production_recall_smoke_dataset
    from eimemory.evaluation.production_recall import normalize_production_recall_dataset

    dataset = _production_recall_smoke_dataset(SimpleNamespace(), scope={})
    normalized = normalize_production_recall_dataset(dataset)
    assert normalized["evaluation_contract"] == "known_item_smoke.v1"
    assert normalized["cases"] == []


def test_generated_contract_survives_evaluation_and_persistence(tmp_path):
    from dataclasses import asdict
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.scheduler.jobs import _production_recall_smoke_dataset
    from eimemory.evaluation.production_recall import run_production_recall_eval

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    try:
        runtime.store.append(RecordEnvelope.create(
            kind="memory", title="Canonical preference",
            summary="Distinct live recall target must remain eligible.",
            scope=scope, source="operator.preference", source_id="pref-1",
            meta={"memory_type": "preference"},
        ))
        dataset = _production_recall_smoke_dataset(runtime, scope=asdict(scope))
        assert dataset["evaluation_contract"] == "known_item_smoke.v1"
        report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
        assert report["evaluation_contract"] == "known_item_smoke.v1"
        assert report["gate_ok"] is False
        assert report["passed_threshold"] is False
        assert report["quality_gate"]["evidence_status"] == "insufficient"
        saved = runtime.store.get_by_id(report["persisted_record_id"])
        assert saved is not None
        assert saved.content["report"]["evaluation_contract"] == "known_item_smoke.v1"
        assert saved.content["report"]["gate_ok"] is False
    finally:
        runtime.close()


def test_smoke_cutoff_skips_same_run_records(tmp_path):
    from dataclasses import asdict
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.scheduler.jobs import _production_recall_smoke_dataset

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    try:
        older = runtime.store.append(RecordEnvelope.create(
            kind="memory", title="Stable target",
            summary="Stable known item must stay eligible after the cutoff.",
            scope=scope, source="operator.preference", source_id="pref-old",
            meta={"memory_type": "preference"},
        ))
        older.time.updated_at = "2020-01-01T00:00:00Z"
        runtime.store.mutate_records_atomically(
            lambda sqlite: (sqlite.upsert(older, commit=False), [older], [])
        )
        fresh = runtime.store.append(RecordEnvelope.create(
            kind="memory", title="Fresh target",
            summary="A record written during this nightly must not be sampled.",
            scope=scope, source="operator.preference", source_id="pref-new",
            meta={"memory_type": "preference"},
        ))
        dataset = _production_recall_smoke_dataset(
            runtime,
            scope=asdict(scope),
            stable_before="2026-01-01T00:00:00Z",
        )
        ids = [case["expected_record_ids"][0] for case in dataset["cases"]]
        assert older.record_id in ids
        assert fresh.record_id not in ids
    finally:
        runtime.close()


def test_evidence_wait_does_not_fail_nightly_but_real_miss_does():
    from eimemory.scheduler.jobs import _aggregate_nightly_ok

    waiting = {
        "recall_quality_gate": {
            "ok": False,
            "blocked_reason": "recall_quality_evidence_incomplete",
            "blocking_metrics": {},
        },
        "production_recall": {"ok": True},
    }
    assert _aggregate_nightly_ok(waiting, [{"step": "production_recall", "ok": True}]) is True
    missing = {
        "recall_quality_gate": {
            "ok": False,
            "blocked_reason": "recall_quality_gate_failed",
            "blocking_metrics": {"hit_at_5": {"actual": 0.7, "threshold": 0.9, "operator": ">="}},
        },
    }
    assert _aggregate_nightly_ok(missing, [{"step": "production_recall", "ok": True}]) is False
