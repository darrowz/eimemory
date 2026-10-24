"""Synthetic, single-call observations; no claim about production recall quality."""
from types import SimpleNamespace

import pytest

from eimemory.evaluation.semantic_recall import evaluate_semantic_recall, score_case, summarize
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.target_trace import current_target_trace, native_target_trace_scope


def fixture(*, provider_count=4, targets=1, native=True):
    scope = ScopeRef(user_id="owner")
    records = [RecordEnvelope.create(kind="memory", title=f"fact-{index}",
        summary=f"independent test fact {index}", scope=scope, source_id="private")
        for index in range(max(provider_count, targets))]
    identity = {"relevance_admission": {"enabled": True}, "candidate_source": {"postgres": {
        "state": "available", "index_verified": True, "query_valid": True, "bypass_reason": ""}}}
    engine = SimpleNamespace(store=object(), effective_identity=lambda: identity)
    calls = []

    def recall(**kwargs):
        calls.append(kwargs)
        context = kwargs["task_context"]
        request = CandidateRequest.create(query=kwargs["query"], scope=kwargs["scope"],
            source_ids=context["source_ids"], target_source_id=context.get("target_source_id"),
            task_context=context, limit=kwargs["limit"])
        if native:
            with native_target_trace_scope(engine, request):
                trace = current_target_trace(engine)
                if trace is not None:
                    trace.observe("provider_returned", records[:provider_count])
                    trace.observe("provider_bounded", records[:provider_count])
                    trace.observe("hydrated", records[:provider_count])
                    trace.drop("filtered", records[0], "preference_query")
                    for stage in ("filtered", "fusion_input", "selector_input", "selector_output", "final_items"):
                        trace.observe(stage, [])
        return SimpleNamespace(items=[], explanation={"retrieval_status": "no_evidence",
            "relevance_selector": {"status": "no_evidence"},
            "target_trace": {"schema": "controlled_target_stage_trace.v1", "status": "completed",
                "stages": [{"stage": "provider_bounded", "target_presence": "present",
                            "observed_count": 1, "window_complete": True}]}})

    by_ref = {record.record_id: record for record in records}
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_exact_ref=lambda ref, **_: by_ref.get(ref)),
        memory=SimpleNamespace(recall=recall, recall_engine=engine))
    case = {"case_id": "held-out-original", "query": "original held-out query", "split": "holdout",
        "intent_group": "unseen-intent", "scope": {"user_id": "owner"}, "source_id": "private",
        "task_context": {"target_source_id": "private"},
        "expected_groups": [[record.record_id] for record in records[:targets]]}
    dataset = {"schema": "semantic_recall_cases.v1", "cases": [case], "candidate_diagnostics": {"k": 5}}
    return runtime, dataset, calls


def test_candidate_hit_and_first_loss_are_from_the_same_original_invocation():
    runtime, dataset, calls = fixture()
    report = evaluate_semantic_recall(runtime, dataset)
    diagnostics = report["samples"][0]["candidate_diagnostics"]
    assert len(calls) == 1
    assert diagnostics["candidate_recall_at_k"] == 1
    assert diagnostics["first_loss"] == {"from_stage": "hydrated", "to_stage": "filtered",
        "drop_reasons": ["preference_query"]}
    assert report["summary"]["candidate_diagnostics"]["known_positive_count"] == 1
    assert report["samples"][0]["metrics"]["hit_at_5"] is False
    assert report["passed"] is False


@pytest.mark.parametrize("options,reason", [
    ({"provider_count": 6}, "candidate_rank_not_observed"),
    ({"targets": 2}, "multiple_expected_refs_not_traced"),
    ({"native": False}, "native_trace_not_completed"),
])
def test_unobserved_candidate_recall_is_unknown_not_zero_or_bundle_claim(options, reason):
    runtime, dataset, calls = fixture(**options)
    report = evaluate_semantic_recall(runtime, dataset)
    diagnostics = report["samples"][0]["candidate_diagnostics"]
    assert len(calls) == 1
    assert diagnostics["candidate_recall_at_k"] is None
    assert diagnostics["reason"] == reason
    coverage = report["summary"]["candidate_diagnostics"]
    assert coverage["known_positive_count"] == 0
    assert coverage["unknown_positive_count"] == 1
    assert coverage["candidate_recall_at_k"] is None


def test_diagnostic_option_does_not_change_query_context_or_ranking():
    runtime, dataset, calls = fixture()
    traced = evaluate_semantic_recall(runtime, dataset)
    ordinary = evaluate_semantic_recall(runtime, {key: value for key, value in dataset.items()
        if key != "candidate_diagnostics"})
    assert len(calls) == 2 and calls[0] == calls[1]
    assert traced["samples"][0]["result_refs"] == ordinary["samples"][0]["result_refs"]
    assert traced["samples"][0]["metrics"] == ordinary["samples"][0]["metrics"]


def test_final_precision_mrr_and_negative_coverage_preserve_delivery_slots():
    metric = score_case(["noise", "noise", "answer", "tail", "tail", "outside"], [["answer"], ["outside"]])
    assert metric["precision_at_1"] == 0
    assert metric["precision_at_5"] == .2
    assert metric["reciprocal_rank"] == pytest.approx(1 / 3)
    assert metric["group_recall"] == .5
    summary = summarize([{"metrics": metric, "latency_ms": 12}])
    assert summary["metrics"]["false_recall_rate"] is None
    assert summary["metrics"]["mrr"] == pytest.approx(1 / 3)
    assert summary["metrics"]["precision_at_5"] == .2


def test_legacy_aggregate_cannot_invent_new_delivery_metrics():
    legacy = score_case(["answer"], [["answer"], ["missing_fact"]])
    for name in ("precision_at_1", "precision_at_5", "reciprocal_rank"):
        legacy.pop(name)
    summary = summarize([{"metrics": legacy, "latency_ms": 12}])
    assert summary["metrics"]["precision_at_1"] is None
    assert summary["metrics"]["precision_at_5"] is None
    assert summary["metrics"]["mrr"] is None


@pytest.mark.parametrize("refs", [[], ["unverified-result"]])
def test_unavailable_negative_cannot_measure_false_recall(refs):
    metric = score_case(refs, [], unavailable=True)
    assert metric["false_recall"] is None
    assert metric["passed"] is False


def test_false_recall_denominator_excludes_unavailable_negative_coverage():
    unavailable = score_case([], [], unavailable=True)
    # Even an older serialized zero cannot turn an unavailable path into an
    # evaluated negative when an aggregate is reconstructed.
    unavailable["false_recall"] = False
    samples = [{"metrics": metric, "latency_ms": 12} for metric in (
        score_case(["answer"], [["answer"]]), score_case([], []),
        score_case(["noise"], []), unavailable,
    )]
    summary = summarize(samples)
    assert summary["metrics"]["false_recall_rate"] == .5
    assert summary["negative_count"] == 3
    assert summary["negative_evaluated_count"] == 2
    assert summary["negative_unavailable_count"] == 1
    assert summary["negative_evaluation_coverage"] == pytest.approx(2 / 3)
    assert summary["metrics"]["unavailable_count"] == 1
    assert summary["passed"] is False


def test_all_unavailable_negative_invocations_report_unknown_false_recall():
    runtime, dataset, calls = fixture()
    dataset["cases"][0]["expected_groups"] = []
    runtime.memory.recall_engine.effective_identity = lambda: {}
    report = evaluate_semantic_recall(runtime, dataset)
    assert len(calls) == 1
    assert report["samples"][0]["semantic_path_verified"] is False
    summary = report["summary"]
    assert summary["metrics"]["false_recall_rate"] is None
    assert summary["negative_count"] == summary["negative_unavailable_count"] == 1
    assert summary["negative_evaluated_count"] == 0
    assert summary["negative_evaluation_coverage"] == 0
    assert "false_recall_rate" in summary["failed_gates"]
    assert "unavailable_count" in summary["failed_gates"]
    assert report["metric_denominators"]["false_recall_rate"] == "verified_semantic_path_negative_cases"
    assert report["passed"] is False


def test_original_query_metrics_keep_the_observed_delivery_slots():
    from eimemory.evaluation.original_query_recall import _metrics
    metric = _metrics(["noise", "noise", "answer", "tail", "tail", "outside"],
                      [{"record_ref": "answer"}, {"record_ref": "outside"}])
    assert metric["recall_at_5"] == .5
    assert metric["precision_at_5"] == .2
    assert metric["reciprocal_rank"] == pytest.approx(1 / 3)
    assert metric["result_refs"] == ["noise", "noise", "answer", "tail", "tail"]


@pytest.mark.parametrize("option", [{"k": True}, {"k": 0}, {"k": 5001}, {"k": 5, "ignored": True}, []])
def test_candidate_diagnostic_configuration_is_validated_before_queries(option):
    runtime, dataset, calls = fixture()
    dataset["candidate_diagnostics"] = option
    with pytest.raises(ValueError, match="semantic_candidate_diagnostics_invalid"):
        evaluate_semantic_recall(runtime, dataset)
    assert not calls


def test_native_sqlite_candidate_trace_uses_one_call_without_claiming_semantic_acceptance(tmp_path, monkeypatch):
    import socket
    from eimemory.api.runtime import Runtime

    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("offline fixture attempted network"))
    runtime = Runtime.create(root=tmp_path)
    try:
        record = runtime.memory.ingest(text="The Nacre archive destination is the amber cabinet.",
            memory_type="fact", title="Nacre archive destination", scope={"user_id": "owner"},
            source="test.offline", source_id="private", force_capture=True)
        original = runtime.memory.recall
        calls = []
        def observed_call(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)
        monkeypatch.setattr(runtime.memory, "recall", observed_call)
        dataset = {"schema": "semantic_recall_cases.v1", "candidate_diagnostics": {"k": 64},
            "cases": [{"case_id": "offline-synthetic", "query": "Nacre archive destination",
                "split": "regression", "intent_group": "offline-synthetic", "scope": {"user_id": "owner"},
                "source_id": "private", "task_context": {"target_source_id": "private"},
                "expected_groups": [[record.record_id]]}]}
        report = evaluate_semantic_recall(runtime, dataset)
        assert len(calls) == 1
        diagnostic = report["samples"][0]["candidate_diagnostics"]
        assert diagnostic["target_trace"]["status"] == "completed"
        assert diagnostic["candidate_recall_at_k"] == 1
        assert report["passed"] is False
        assert report["samples"][0]["semantic_path_verified"] is False
    finally:
        runtime.close()
