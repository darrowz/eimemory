"""Evolution summaries retain source-bound conclusions without private inputs."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_knowledge_gap_review import registered, _evidence, _run, _model, TEXT


SCOPE = ScopeRef(agent_id="private-agent", workspace_id="private-workspace::channel::hermes",
                 user_id="private-member")
PRIVATE = "private-source-body-and-token"


def _native_ref(prefix, index=1):
    return prefix + "_" + f"{index:032x}"


def _knowledge_review(**overrides):
    return {"schema": "knowledge.gap_applicability_review.v1", "status": "linked",
            "eligible_count": 1, "reviewed_count": 1, "applicable_count": 1,
            "completed_count": 1, "deferred_count": 0, "work_item_ids": ["1" * 40],
            "review_receipt_ids": [_native_ref("knowledge_gap_review")], "reason_counts": {},
            "links": [{"link_id": _native_ref("knowledge_link"), "link_digest": "1" * 64,
                       "review_receipt_id": _native_ref("knowledge_gap_review"),
                       "environment_context": {"host": PRIVATE}}], **overrides}


def _dynamic_report(result_status="evaluated", **proposal_overrides):
    return {"ok": True, "execution": {"ok": True, "results": [{
        "status": result_status, "work_item_id": "1" * 40, "candidate_proposal": {
            "ok": True, "status": "proposal_ready", "schema": "dynamic_capability_patch_context.v1",
            "transaction_id": "dynamic-gap-" + "2" * 32, "incident_id": "dynamic-gap-" + "3" * 32,
            "hypothesis_id": _native_ref("capability_hypothesis"),
            "qualifying_feedback_id": _native_ref("capability_hypothesis_feedback"),
            "evaluation_artifact_ids": [_native_ref("capability_hypothesis_evaluation")],
            "transaction_owner": "promotion_manager", "origin": "user_reported",
            "qualifies_for_product_completion": False, "known_before_detection": True,
            "prior_user_reported": True, "manual_bootstrap": False,
            "repository": {"root": PRIVATE}, "scope": asdict(SCOPE), **proposal_overrides},
    }]}}


def test_actual_controlled_retrieval_conclusion_survives_durable_nightly_readback(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = RecordEnvelope.create(kind="memory", title="Atlas project notebook",
            summary=PRIVATE, content={"text": "The Atlas project uses green notebooks.", "memory_type": "fact"},
            source="operator.memory", source_id="hermes", scope=SCOPE,
            meta={"memory_type": "fact", "force_capture": True})
        runtime.store.append(source)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        measured = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])
        assert measured["pass_count"] == 1
        measured["dataset_source"] = "controlled_records"
        measured["controlled_context"] = {"channel": "hermes", "reason": ""}
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": measured}, [])
        result = diagnostics["memory_benchmark"]
        assert result["dataset_source"] == "controlled_records"
        assert result["controlled_benchmark_passed_threshold"] is True
        assert result["formal_business_evidence"] is False
        assert result["certifies_natural_queries"] is result["certifies_no_answer"] is False
        assert diagnostics["memory_benchmark_accepted"] is False
        assert result["evaluated_case_count"] == result["pass_count"] == 1
        assert result["case_results"][0]["passed"] is True
        assert result["dataset_generation"]["source_refs"][0]["record_id"] == source.record_id
        assert "scope" not in result["dataset_generation"]["source_refs"][0]
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        saved_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        saved = runtime.store.get_by_id(saved_id).content["nightly_diagnostics"]
        assert saved == diagnostics
        contract = build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"]
        assert contract["memory_benchmark"] == result
    serialized = json.dumps(result)
    assert PRIVATE not in serialized
    assert "green notebooks" not in serialized
    assert "private-member" not in serialized


def test_missing_controlled_and_knowledge_counters_stay_unknown():
    report = {"memory_eval_ci": {"ok": True, "dataset_source": "controlled_records",
                               "controlled_context": {"reason": "memory_benchmark_channel_binding_missing"}},
              "capability_hypothesis_producer": {"ok": True, "status": "no_eligible_evidence",
                                                "knowledge_link_reviews": [{}]}}
    diagnostics = nightly_result_diagnostics(report, [])
    memory = diagnostics["memory_benchmark"]
    assert memory["retrieval_case_count"] is memory["evaluated_case_count"] is None
    assert memory["formal_business_evidence"] is memory["controlled_benchmark_passed_threshold"] is None
    assert memory["controlled_context"]["reason"] == "memory_benchmark_channel_binding_missing"
    producer = diagnostics["capability_hypothesis_producer"]
    assert producer["created_count"] is producer["reused_count"] is None
    review = producer["knowledge_link_reviews"][0]
    assert review["eligible_count"] is review["reviewed_count"] is review["applicable_count"] is None
    assert review["review_receipt_ids"] is review["reason_counts"] is None
    assert review["links_truncated"] is None


def test_native_controlled_cutoff_and_expired_sources_retain_specific_diagnostics(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        for index, metadata in enumerate(({}, {"valid_until": "2000-01-01T00:00:00Z"},
                                          {"valid_from": "2100-01-01T00:00:00Z"}, {})):
            source = RecordEnvelope.create(kind="memory", title=f"Atlas project notebook {index}",
                summary=PRIVATE, content={"text": f"Atlas notebook {index} is green.", "memory_type": "fact"},
                source="operator.memory", source_id="hermes", scope=SCOPE,
                meta={"memory_type": "fact", "force_capture": True, **metadata})
            source.time.created_at = "2026-10-10T00:00:00Z" if index < 3 else "2026-10-12T00:00:00Z"
            runtime.store.append(source)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"],
                                                     stable_before="2026-10-11T00:00:00Z")
        assert len(dataset["cases"]) == 1
        report = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])
        report["dataset_source"] = "controlled_records"
        generation = nightly_result_diagnostics({"memory_eval_ci": report}, [])["memory_benchmark"]["dataset_generation"]
        assert generation["same_run_skipped_count"] == 1
        assert generation["stable_before"] == "2026-10-11T00:00:00Z"
        assert generation["skipped_reason_counts"] == {
            "source_record_expired": 1, "source_record_not_yet_valid": 1,
            "source_not_stable_before_cutoff": 1}
        assert generation["selection_truncated"] is False


def test_knowledge_review_references_are_bounded_and_private_payloads_excluded():
    review = _knowledge_review(
        review_receipt_ids=[_native_ref("knowledge_gap_review", index) for index in range(25)] + [PRIVATE],
        reason_counts={"applicability_review_quote_invalid": 2, "applicability_review_revoked": 1,
                       PRIVATE: 3, "knowledge_evidence_unavailable": True},
        source_review={"reviewer": PRIVATE}, canonical_window=PRIVATE, scope=asdict(SCOPE))
    report = {"capability_hypothesis_producer": {"ok": True, "status": "reused", "created": [],
        "reused": [{"hypothesis_id": _native_ref("capability_hypothesis"),
                    "candidate_bounds_status": "evaluation_only",
                    "candidate_bounds_reason": "no_registered_writable_candidate_bounds",
                    "code_changes_authorized": False, "model_text": PRIVATE}],
        "knowledge_link_reviews": [review]}}
    snapshot = deepcopy(report)
    producer = nightly_result_diagnostics(report, [])["capability_hypothesis_producer"]
    measured = producer["knowledge_link_reviews"][0]
    assert producer["created_count"] == 0 and producer["reused_count"] == 1
    assert producer["candidate_bounds"][0]["code_changes_authorized"] is False
    assert measured["review_receipt_ids_truncated"] is True
    assert measured["completed_count"] == 1 and measured["deferred_count"] == 0
    assert measured["work_item_ids"] == ["1" * 40]
    assert len(measured["review_receipt_ids"]) == 20
    assert measured["invalid_review_receipt_id_count"] == 1
    assert measured["reason_counts"] == {"applicability_review_quote_invalid": 2,
        "applicability_review_revoked": 1, "reason_not_allowlisted": 3}
    assert measured["link_refs"][0]["review_receipt_id"] == _native_ref("knowledge_gap_review")
    assert PRIVATE not in json.dumps(producer)
    assert "private-member" not in json.dumps(producer)
    assert report == snapshot


def test_proposal_context_is_reported_on_success_without_claiming_autonomous_completion():
    report = {"dynamic_capability_evolution": _dynamic_report()}
    snapshot = deepcopy(report)
    result = nightly_result_diagnostics(report, [])
    assert result["execution_ok"] is True and result["failed_steps"] == []
    context = result["dynamic_capability_evolution"]["patch_contexts"][0]
    assert result["dynamic_capability_evolution"]["reason_counts"] == {}
    assert context["transaction_id"] == "dynamic-gap-" + "2" * 32
    assert context["hypothesis_id"] == _native_ref("capability_hypothesis")
    assert context["qualifying_feedback_id"] == _native_ref("capability_hypothesis_feedback")
    assert context["evaluation_artifact_ids"] == [_native_ref("capability_hypothesis_evaluation")]
    assert context["qualifies_for_product_completion"] is False
    assert context["known_before_detection"] is context["prior_user_reported"] is True
    assert context["manual_bootstrap"] is False
    assert context["origin"] == "user_reported"
    assert context["transaction_owner"] == "promotion_manager"
    assert PRIVATE not in json.dumps(result)
    assert "private-member" not in json.dumps(result)
    assert report == snapshot


def test_context_blocked_reason_is_exact_and_unknown_errors_are_not_echoed():
    report = {"dynamic_capability_evolution": _dynamic_report(result_status="blocked",
        status="blocked", reason="dynamic_patch_durable_evidence_changed", transaction_id=PRIVATE,
        hypothesis_id=PRIVATE, evaluation_artifact_ids=[PRIVATE])}
    report["dynamic_capability_evolution"]["execution"]["results"][0]["reason"] = "dynamic_patch_durable_evidence_changed"
    result = nightly_result_diagnostics(report, [])["dynamic_capability_evolution"]
    assert result["reason_counts"] == {"dynamic_patch_durable_evidence_changed": 1}
    context = result["patch_contexts"][0]
    assert context["reason"] == "dynamic_patch_durable_evidence_changed"
    assert context["transaction_id"] is context["hypothesis_id"] is None
    assert context["evaluation_artifact_ids"] == []
    assert context["invalid_evaluation_artifact_id_count"] == 1
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("source_id", ["hermes", "configured-private-member"])
def test_native_controlled_failure_source_and_measured_stages_survive_nightly_readback(tmp_path, monkeypatch, source_id):
    from test_controlled_memory_benchmark import _ControlledSource, _use_native_source
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    with Runtime.create(root=tmp_path) as runtime:
        source = RecordEnvelope.create(kind="memory", title="Atlas project notebook",
            summary=PRIVATE, content={"text": "Atlas project notebook is green.", "memory_type": "fact"},
            source="operator.memory", source_id=source_id, scope=SCOPE,
            meta={"memory_type": "fact", "force_capture": True})
        runtime.store.append(source)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=[source_id])
        _use_native_source(runtime, _ControlledSource(runtime.store))
        measured = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=[source_id])
        measured["dataset_source"] = "controlled_records"
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": measured}, [])
        benchmark = diagnostics["memory_benchmark"]
        assert benchmark["formal_business_evidence"] is False
        assert benchmark["controlled_benchmark_passed_threshold"] is False
        assert diagnostics["memory_benchmark_accepted"] is False
        failure = benchmark["failed_case_refs"][0]
        assert failure["failure_reason"] == "known_item_missing"
        assert failure["returned_record_count"] == 0 and failure["evaluable"] is True
        assert failure["source_binding"]["record_id"] == source.record_id
        expected_source = ({"source_id": source_id} if source_id == "hermes" else
                           {"source_id_digest": sha256(source_id.encode()).hexdigest()})
        assert expected_source.items() <= failure["source_binding"].items()
        retrieval = failure["retrieval_diagnostics"]
        assert retrieval["source_partition"]["target_source"] == expected_source
        assert retrieval["source_availability"]["status"] == "available"
        assert retrieval["engine"]["candidate_count"] == 0
        assert retrieval["empty_result_cause"] == "candidate_collection_empty"
        assert retrieval["measurement_origin"] == "controlled_runtime_bundle"
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        receipt_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        readback = runtime.store.get_by_id(receipt_id).content["nightly_diagnostics"]
        assert readback["memory_benchmark"]["failed_case_refs"] == benchmark["failed_case_refs"]
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    serialized = json.dumps(diagnostics)
    assert PRIVATE not in serialized and "private-member" not in serialized and "Atlas" not in serialized


def test_failed_case_projection_bounds_privacy_and_missing_stage_observations():
    raw = {"schema": "retrieval_stage_diagnostics.v1", "measurement_origin": "controlled_runtime_bundle",
           "retrieval_status": "no_evidence", "empty_result_cause": "candidate_collection_empty",
           "engine": {"candidate_count": None, "candidate_limit": True, "elapsed_ms": float("nan"),
                      "drops": {"authority_validation_timeout": -1, PRIVATE: 1}, "source_names": [PRIVATE]},
           "selector": {"selected_count": False, "verification_session_id": PRIVATE},
           "source_availability": {"status": "available", "source_searches": True},
           "source_partition": {"scope": asdict(SCOPE), "target_source_id": PRIVATE, "source_ids": ["\ud800"]},
           "assistance": {"status": "unavailable", "calls": None, "candidate_count": False,
                          "elapsed_ms": float("nan"), "reason": PRIVATE, "proofs": [{"quote": PRIVATE}]},
           "verifier_boundary": {"visible_candidate_count": "unknown", "reason": PRIVATE},
           "pipeline": [{"name": PRIVATE}, {"name": "retrieve", "retrieved_count": -1, "query": PRIVATE}]}
    samples = [{"passed": False, "evaluable": True, "case_id": "controlled-memory-" + f"{index:024x}",
                "failure_reason": "known_item_missing", "source_binding": {"record_id": "mem_" + "1" * 32,
                    "source_id": "hermes", "scope": asdict(SCOPE)}, "retrieval_diagnostics": raw}
               for index in range(7)]
    report = {"memory_eval_ci": {"ok": True, "dataset_source": "controlled_records", "samples": samples}}
    snapshot = deepcopy(report)
    memory = nightly_result_diagnostics(report, [])["memory_benchmark"]
    assert len(memory["failed_case_refs"]) == 5
    assert memory["failed_case_ref_count"] == 7 and memory["failed_case_refs_truncated"] is True
    failure = memory["failed_case_refs"][0]
    assert failure["returned_record_count"] is None
    observed = failure["retrieval_diagnostics"]
    assert observed["engine"]["candidate_count"] is None
    assert observed["engine"]["candidate_limit"] is observed["engine"]["elapsed_ms"] is None
    assert observed["selector"]["selected_count"] is None
    assert observed["source_availability"]["status"] == "unknown"
    assert observed["source_availability"]["source_searches"] is None
    assert observed["source_availability"]["source_budget_exhausted"] is None
    assert observed["source_partition"]["source_refs"] == [{"source_id": None}]
    assert failure["source_binding"]["source_id"] == "hermes"
    assert observed["empty_result_cause"] == "empty_origin_unknown"
    assert "calls" not in observed["assistance"] and "candidate_count" not in observed["assistance"]
    assert observed["verifier_boundary"]["visible_candidate_count"] is None
    assert PRIVATE not in json.dumps(memory) and "private-member" not in json.dumps(memory)
    # NaNs compare by identity after deepcopy; inspect nonnumeric portions and
    # the original invalid telemetry rather than treating JSON as normalized.
    assert report["memory_eval_ci"]["samples"][0]["retrieval_diagnostics"] is raw
    assert snapshot["memory_eval_ci"]["samples"][0]["source_binding"] == samples[0]["source_binding"]


def test_native_knowledge_inventory_progress_and_certificate_survive_nightly_readback(registered):
    runtime, _, _, _ = registered
    from test_dynamic_capability_evals import SCOPE as capability_scope
    for index in range(7):
        _evidence(registered, suffix=f"progress-{index}")
    calls = []
    def model(system, prompt):
        output = json.loads(_model(system, prompt))
        calls.append(output["input_digest"])
        if len(calls) > 1:
            output.update(applicability="not_applicable", quotes=[])
        return json.dumps(output)
    first = _run(registered, executor=model)
    assert first["status"] == "scanning" and first["inventory"]["complete"] is False
    first_diagnostics = nightly_result_diagnostics({"capability_hypothesis_producer": {
        "ok": True, "status": "no_eligible_evidence", "knowledge_link_reviews": [first]}}, [])
    progress = first_diagnostics["capability_hypothesis_producer"]["knowledge_link_reviews"][0]
    assert progress["status"] == "scanning"
    assert progress["inventory"]["pending_count"] == 3
    assert progress["inventory"]["certificate_ref"] is None
    assert progress["reason_counts"]["knowledge_review_budget_deferred"] == 3
    second = _run(registered, executor=model)
    assert len(calls) == 7 and second["status"] == "linked"
    diagnostics = nightly_result_diagnostics({"capability_hypothesis_producer": {
        "ok": True, "status": "produced", "knowledge_link_reviews": [second]}}, [])
    result = diagnostics["capability_hypothesis_producer"]["knowledge_link_reviews"][0]
    inventory = result["inventory"]
    assert inventory["complete"] is inventory["inventory_complete"] is inventory["changes_complete"] is True
    assert inventory["pending_count"] == inventory["unknown_count"] == 0
    assert inventory["manifest_record_count"] == 7
    assert inventory["input_watermark"] == inventory["current_watermark"]
    certificate_ref = inventory["certificate_ref"]
    certificate = runtime.store.get_by_id(certificate_ref, scope=ScopeRef.from_dict(capability_scope), exact_scope=True)
    assert certificate.content["schema"] == inventory["schema"]
    assert result["link_refs"][0]["inventory_certificate_ref"] == certificate_ref
    summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
    summary["nightly_diagnostics"] = diagnostics
    saved_id = persist_supervisor_summary(runtime, scope=capability_scope, summary=summary).record_id
    root = runtime.store.root
    runtime.close()
    with Runtime.create(root=root) as reopened:
        saved = reopened.store.get_by_id(saved_id).content["nightly_diagnostics"]
        assert saved == diagnostics
        assert reopened.store.get_by_id(certificate_ref).content["schema"] == inventory["schema"]
    assert TEXT not in json.dumps(diagnostics)
    assert "paper.example.test" not in json.dumps(diagnostics)
    assert "storage_key" not in json.dumps(diagnostics)


def test_inventory_missing_and_malformed_fields_stay_unknown_and_old_shape_is_preserved():
    old = _knowledge_review()
    report = {"capability_hypothesis_producer": {"ok": True, "status": "diagnosed",
        "knowledge_link_reviews": [old, _knowledge_review(status="scanning", inventory={
            "schema": "knowledge.gap_inventory_scan.v1", "complete": True, "pending_count": False,
            "unknown_count": -1, "input_watermark": 2 ** 50, "current_watermark": None,
            "scan_key": PRIVATE, "cursor_digest": "1" * 64, "next_transition": PRIVATE,
            "certificate_ref": PRIVATE, "cursor": PRIVATE, "scope": asdict(SCOPE)},
            reason_counts={"knowledge_inventory_changes_pending": 1,
                           "inventory_scan_state_conflict": 1, PRIVATE: 1})]}}
    snapshot = deepcopy(report)
    reviews = nightly_result_diagnostics(report, [])["capability_hypothesis_producer"]["knowledge_link_reviews"]
    assert "inventory" not in reviews[0]
    inventory = reviews[1]["inventory"]
    assert inventory["pending_count"] is inventory["unknown_count"] is inventory["current_watermark"] is None
    assert inventory["page_record_count"] is inventory["changes_complete"] is inventory["inventory_complete"] is None
    assert inventory["scan_key"] is inventory["certificate_ref"] is inventory["next_transition"] is None
    assert inventory["input_watermark"] == 2 ** 50
    assert reviews[1]["reason_counts"] == {"knowledge_inventory_changes_pending": 1,
        "inventory_scan_state_conflict": 1, "reason_not_allowlisted": 1}
    assert PRIVATE not in json.dumps(reviews) and "private-member" not in json.dumps(reviews)
    assert report == snapshot


@pytest.mark.parametrize("target_returned", [True, False])
def test_native_single_target_trace_and_query_material_survive_both_durable_reports(
    tmp_path, monkeypatch, target_returned,
):
    from eimemory.api.memory import MemoryAPI
    from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.retrieval.target_trace import safe_target_trace_report
    from eimemory.scheduler.jobs import _memory_eval_report_record
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")

    class MeasuredVectorSource(SQLiteCandidateSource):
        def __init__(self, store, records):
            super().__init__(store)
            self.records = records

        def search(self, request):
            records = [record for record in self.records if record.kind in request.kinds]
            return CandidateBatch(hits=tuple(CandidateHit(
                ref=CandidateRef(record_id=record.record_id, scope=ExactScope.from_scope(record.scope),
                                 source_id=record.source_id),
                source_rank=index + 1, source_score=0.9,
                component_hints={"vector_score": 0.9, "_provider_rank_is_hybrid": True},
                evidence_hints=("vector_match",)) for index, record in enumerate(records)),
                diagnostics={"source_name": "sqlite", "candidate_count": len(records),
                    "candidate_limit": request.limit, "elapsed_ms": 0.1, "fallback": False, "drops": {}})

    with Runtime.create(root=tmp_path) as runtime:
        target = RecordEnvelope.create(kind="memory", title="a" * 350, summary="a" * 350,
            content={"text": "a" * 350, "memory_type": "fact"}, scope=SCOPE,
            source="operator.memory", source_id="hermes", meta={"memory_type": "fact", "force_capture": True})
        runtime.store.append(target)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        original_query = dataset["cases"][0]["query"]
        records = [target] if target_returned else []
        for index in range(13 if target_returned else 14):
            unrelated = RecordEnvelope.create(kind="memory", title=f"Synthetic unrelated location {index}",
                summary="Different stellar location map.",
                content={"text": "Different stellar location map.", "memory_type": "fact"}, scope=SCOPE,
                source="operator.memory", source_id="hermes", meta={"memory_type": "fact", "force_capture": True})
            runtime.store.append(unrelated)
            records.append(unrelated)
        runtime.memory = MemoryAPI(runtime.store, recall_engine=GovernedRecallEngine(
            store=runtime.store, candidate_source=MeasuredVectorSource(runtime.store, records)))
        measured = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])
        measured["dataset_source"] = "controlled_records"
        sample = measured["samples"][0]
        assert dataset["cases"][0]["query"] == original_query
        assert sample["failure_reason"] == "known_item_missing"
        assert sample["returned_record_count"] == 0
        assert sample["retrieval_diagnostics"]["selector"]["input_count"] == 14
        assert sample["retrieval_diagnostics"]["selector"]["dropped_reasons"] == {"grounding": 14}
        trace = sample["retrieval_diagnostics"]["target_trace"]
        assert trace["schema"] == "controlled_target_stage_trace.v1"
        assert trace["does_not_certify_answer"] is True
        stages = {stage["stage"]: stage for stage in trace["stages"]}
        assert stages["provider_returned"]["target_presence"] == (
            "present" if target_returned else "absent_in_observed_window")
        assert stages["selector_input"]["target_presence"] == (
            "present" if target_returned else "absent_in_observed_window")
        if target_returned:
            assert "grounding" in stages["selector_input"]["drop_reasons"]
            assert stages["selector_input"]["grounding_score"] is not None
            assert stages["selector_input"]["grounding_threshold"] is not None
        else:
            assert stages["selector_input"]["drop_reasons"] == []
            assert stages["selector_input"]["grounding_score"] is None
        assert len(trace["stages"]) <= 8
        native_receipt = _memory_eval_report_record(measured, scope=SCOPE)
        runtime.store.append(native_receipt)
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": measured}, [])
        failure = diagnostics["memory_benchmark"]["failed_case_refs"][0]
        assert failure["retrieval_diagnostics"]["target_trace"] == safe_target_trace_report(trace)
        assert failure["query_generation"] == sample["query_generation"]
        assert failure["query_generation"]["material_field"] == "title"
        assert failure["query_generation"]["material_chars"] == 350
        assert failure["query_generation"]["query_chars"] == len(original_query)
        assert failure["query_generation"]["truncated"] is True
        assert diagnostics["memory_benchmark_accepted"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        supervisor_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        persisted_sample = runtime.store.get_by_id(native_receipt.record_id).content["report"]["samples"][0]
        assert persisted_sample["retrieval_diagnostics"]["target_trace"] == trace
        assert persisted_sample["query_generation"] == sample["query_generation"]
        persisted_diagnostics = runtime.store.get_by_id(supervisor_id).content["nightly_diagnostics"]
        assert persisted_diagnostics == diagnostics
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    serialized = json.dumps(diagnostics)
    assert "a" * 100 not in serialized and "Different stellar" not in serialized
    assert "private-member" not in serialized and "private-workspace" not in serialized
    assert "query_digest" not in serialized and "scope" not in json.dumps(trace)


def test_target_trace_projection_is_conditional_bounded_and_does_not_invent_old_membership():
    invalid_trace = {"schema": "controlled_target_stage_trace.v1", "status": "completed",
        "does_not_certify_answer": False, "query": PRIVATE, "scope": asdict(SCOPE),
        "stages": [{"stage": "selector_output", "target_presence": "present", "observed_count": True,
            "inspected_count": -1, "window_complete": PRIVATE, "truncated": 1,
            "drop_reasons": ["grounding", PRIVATE], "grounding_score": float("nan"),
            "grounding_threshold": -1, "candidate_body": PRIVATE},
            {"stage": PRIVATE, "target_presence": PRIVATE}] * 10}
    sample = {"passed": False, "case_id": "controlled-memory-" + "1" * 24,
        "source_binding": {"record_id": "mem_" + "1" * 32, "source_id": "hermes"},
        "query_generation": {"material_field": PRIVATE, "material_chars": None, "query_chars": False,
                             "truncated": PRIVATE, "query": PRIVATE},
        "retrieval_diagnostics": {"target_trace": invalid_trace}}
    diagnostics = nightly_result_diagnostics({"memory_eval_ci": {"ok": True, "dataset_source": "controlled_records",
        "samples": [sample, {key: value for key, value in sample.items() if key not in {
            "query_generation", "retrieval_diagnostics"}}]}}, [])
    failures = diagnostics["memory_benchmark"]["failed_case_refs"]
    assert "query_generation" not in failures[1] and "retrieval_diagnostics" not in failures[1]
    generation = failures[0]["query_generation"]
    assert generation["material_chars"] is generation["query_chars"] is generation["truncated"] is None
    trace = failures[0]["retrieval_diagnostics"]["target_trace"]
    assert trace["does_not_certify_answer"] is True
    assert len(trace["stages"]) <= 8
    for stage in trace["stages"]:
        assert stage["observed_count"] is stage["inspected_count"] is stage["window_complete"] is stage["truncated"] is None
        assert stage["grounding_score"] is stage["grounding_threshold"] is None
        assert PRIVATE not in stage["drop_reasons"]
    assert PRIVATE not in json.dumps(diagnostics) and "private-member" not in json.dumps(diagnostics)


@pytest.mark.parametrize("boundary,reason", [
    ("capture_rejected", "source_record_capture_rejected"),
    ("episode_evidence", "source_record_episode_evidence"),
    ("evidence_only", "source_record_evidence_only"),
])
@pytest.mark.parametrize("report_mode", ["generated", "provided_case"])
def test_native_source_eligibility_reasons_survive_generation_and_old_case_durable_readback(
    tmp_path, monkeypatch, boundary, reason, report_mode,
):
    from eimemory.scheduler.jobs import _memory_eval_report_record
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    with Runtime.create(root=tmp_path) as runtime:
        target = RecordEnvelope.create(kind="memory", title="Atlas project green notebook",
            summary=PRIVATE, content={"text": "Atlas project uses a green notebook.", "memory_type": "fact"},
            scope=SCOPE, source="operator.memory", source_id="hermes",
            meta={"memory_type": "fact", "force_capture": True})
        runtime.store.append(target)
        original = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        assert len(original["cases"]) == 1
        if boundary == "capture_rejected":
            target.meta["quality"] = {"capture_decision": "reject"}
        elif boundary == "episode_evidence":
            target.meta["capture_origin"] = "turn_sync"
        else:
            target.source = "operator.diagnostic"
        runtime.store.append(target)
        retained = RecordEnvelope.create(kind="memory", title="Orchid project shipment confirmed",
            summary="The Orchid project shipment was confirmed.",
            content={"text": "The Orchid project shipment was confirmed.", "memory_type": "episodic"},
            scope=SCOPE, source="eimemory.l1", source_id="hermes",
            meta={"memory_type": "episodic", "force_capture": True})
        runtime.store.append(retained)
        generated = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        assert [case["source_binding"]["record_id"] for case in generated["cases"]] == [retained.record_id]
        assert generated["dataset_generation"]["skipped_reason_counts"] == {reason: 1}
        current_report = run_controlled_memory_benchmark(runtime, generated, scope=SCOPE, source_ids=["hermes"])
        old_case_report = run_controlled_memory_benchmark(runtime, original, scope=SCOPE, source_ids=["hermes"])
        assert current_report["pass_count"] == 1
        assert old_case_report["samples"][0]["failure_reason"] == reason
        assert old_case_report["not_evaluable_count"] == 1 and old_case_report["pass_count"] == 0
        assert old_case_report["controlled_benchmark_passed_threshold"] is False
        assert "retrieval_diagnostics" not in old_case_report["samples"][0]
        for measured in (current_report, old_case_report):
            measured["dataset_source"] = "controlled_records"
            receipt = _memory_eval_report_record(measured, scope=SCOPE)
            runtime.store.append(receipt)
            diagnostics = nightly_result_diagnostics({"memory_eval_ci": measured}, [])
            assert diagnostics["memory_benchmark_accepted"] is False
            if measured is (current_report if report_mode == "generated" else old_case_report):
                summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
                summary["nightly_diagnostics"] = diagnostics
                supervisor_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
                selected_receipt_id, selected_report, selected_diagnostics = receipt.record_id, measured, diagnostics
        current_diagnostics = nightly_result_diagnostics({"memory_eval_ci": current_report}, [])["memory_benchmark"]
        assert current_diagnostics["dataset_generation"]["skipped_reason_counts"] == {reason: 1}
        old_diagnostics = nightly_result_diagnostics({"memory_eval_ci": old_case_report}, [])["memory_benchmark"]
        assert old_diagnostics["failed_case_refs"][0]["failure_reason"] == reason
        assert "retrieval_diagnostics" not in old_diagnostics["failed_case_refs"][0]
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(selected_receipt_id).content["report"] == selected_report
        assert runtime.store.get_by_id(supervisor_id).content["nightly_diagnostics"] == selected_diagnostics
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == selected_diagnostics
        assert PRIVATE not in json.dumps(selected_diagnostics)


@pytest.mark.parametrize("path", ["gate_reject", "source_window_miss"])
def test_native_sqlite_source_trace_survives_failed_case_and_both_durable_readbacks(tmp_path, monkeypatch, path):
    from eimemory.llm.command_client import CommandLLMClient
    from eimemory.retrieval.target_trace import safe_target_trace_report
    from eimemory.scheduler.jobs import _memory_eval_report_record
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    monkeypatch.setattr(CommandLLMClient, "complete", lambda *args, **kwargs: pytest.fail("Model call prohibited"))
    scope = SCOPE if path == "gate_reject" else ScopeRef("synthetic", "source-audit", "isolated", "owner")
    with Runtime.create(root=tmp_path) as runtime:
        title = "Atlas project notebook" if path == "gate_reject" else "Shared Orchid project archive"
        text = "Atlas project uses green notebooks." if path == "gate_reject" else title
        target = RecordEnvelope.create(kind="memory", title=title,
            summary=text, content={"text": text, "memory_type": "fact"},
            scope=scope, source="operator.memory", source_id="hermes",
            meta={"memory_type": "external_knowledge" if path == "gate_reject" else "fact", "force_capture": True})
        target.record_id = "mem_" + "f" * 32
        runtime.store.append(target)
        dataset = build_controlled_memory_benchmark(runtime, scope=scope, source_ids=["hermes"])
        if path == "source_window_miss":
            # Same-title neighbors exceed the existing bounded identity and
            # ranking windows. Distinct structured contexts keep result dedupe
            # from replacing scored candidates with an identity-only sibling.
            # This synthetic miss is not a production cause.
            for index in range(48):
                neighbor = RecordEnvelope.create(kind="memory", title=title, summary=text,
                    content={"text": text, "memory_type": "fact", "fixture_variant": index}, scope=scope,
                    source="operator.memory", source_id="hermes", meta={"memory_type": "fact", "force_capture": True})
                neighbor.record_id = "mem_" + f"{index:032x}"
                runtime.store.append(neighbor)
        measured = run_controlled_memory_benchmark(runtime, dataset, scope=scope, source_ids=["hermes"])
        measured["dataset_source"] = "controlled_records"
        sample = measured["samples"][0]
        assert sample["failure_reason"] == "known_item_missing", {
            "failure_reason": sample.get("failure_reason"),
            "retrieval_status": sample.get("retrieval_diagnostics", {}).get("retrieval_status"),
            "engine": sample.get("retrieval_diagnostics", {}).get("engine"),
            "source_availability": sample.get("retrieval_diagnostics", {}).get("source_availability"),
        }
        assert sample["evaluable"] is True and sample["passed"] is False
        if path == "gate_reject":
            assert sample["returned_record_count"] == 0
            assert sample["retrieval_diagnostics"]["online_gate"]["blocked_counts"] == {"external_knowledge_untrusted": 1}
        trace = sample["retrieval_diagnostics"]["target_trace"]
        source = trace["source_trace"]
        assert source["schema"] == "sqlite_target_source_trace.v1" and source["status"] == "completed"
        assert source["does_not_certify_answer"] is True
        stages = {stage["stage"]: stage for stage in source["stages"]}
        presence = "present" if path == "gate_reject" else "absent_in_observed_window"
        assert stages["identity_index_rows"]["target_presence"] == presence
        assert stages["identity_verified"]["target_presence"] == presence
        assert stages["returned_hits"]["target_presence"] == presence
        if path == "source_window_miss":
            assert stages["hybrid_collected"]["target_presence"] == "present"
            assert stages["score_ref_binding"]["target_presence"] == "present"
            assert stages["score_ref_binding"]["binding_status"] == "bound"
            assert stages["merged_hits"]["target_presence"] == "present"
            assert "source_return_limit" in stages["returned_hits"]["drop_reasons"]
        assert len(stages) == 10
        assert set(source["collection_arms"]) == {"fts", "anchor", "lane_seed", "recent"}
        assert all(status in {"unknown", "completed", "incomplete"} for status in source["collection_arms"].values())
        receipt = _memory_eval_report_record(measured, scope=scope)
        runtime.store.append(receipt)
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": measured}, [])
        projected = diagnostics["memory_benchmark"]["failed_case_refs"][0]["retrieval_diagnostics"]["target_trace"]
        assert projected == safe_target_trace_report(trace)
        assert projected["source_trace"] == source
        assert diagnostics["memory_benchmark_accepted"] is False
        assert diagnostics["memory_benchmark"]["formal_business_evidence"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        supervisor_id = persist_supervisor_summary(runtime, scope=scope, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        saved_sample = runtime.store.get_by_id(receipt.record_id).content["report"]["samples"][0]
        assert saved_sample["retrieval_diagnostics"]["target_trace"]["source_trace"] == source
        assert runtime.store.get_by_id(supervisor_id).content["nightly_diagnostics"] == diagnostics
        assert build_supervisor_contract(runtime, scope=scope)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    serialized = json.dumps(diagnostics)
    assert PRIVATE not in serialized and "private-member" not in serialized and "Atlas project" not in serialized
    assert "storage_key" not in serialized and "query_digest" not in serialized


def test_source_trace_projection_keeps_old_absence_and_unknown_fields_without_private_history():
    def sample(trace):
        return {"passed": False, "case_id": "controlled-memory-" + "1" * 24,
                "source_binding": {"record_id": "mem_" + "1" * 32, "source_id": "hermes"},
                "retrieval_diagnostics": {"target_trace": trace}}
    legacy = {"schema": "controlled_target_stage_trace.v1", "status": "completed", "stages": []}
    with_source = {**legacy, "source_trace": {"schema": "sqlite_target_source_trace.v1",
        "status": PRIVATE, "does_not_certify_answer": False,
        "collection_arms": {"fts": PRIVATE, "query": PRIVATE},
        "stages": [{"stage": "hybrid_collected", "target_presence": PRIVATE, "observed_count": False,
            "inspected_count": -1, "window_complete": PRIVATE, "truncated": 1,
            "drop_reasons": [PRIVATE], "source_score": 2 ** 10000, "source_rank": True,
            "binding_status": PRIVATE, "record_id": PRIVATE, "query": PRIVATE, "storage_key": PRIVATE},
            {"stage": PRIVATE, "target_presence": "present"}] * 20,
        "raw_scope": asdict(SCOPE), "query": PRIVATE}}
    diagnostic = nightly_result_diagnostics({"memory_eval_ci": {"ok": True, "dataset_source": "controlled_records",
        "samples": [sample(legacy), sample(with_source)]}}, [])["memory_benchmark"]
    traces = [failure["retrieval_diagnostics"]["target_trace"] for failure in diagnostic["failed_case_refs"]]
    assert "source_trace" not in traces[0]
    source = traces[1]["source_trace"]
    assert source["status"] == "not_observed" and source["does_not_certify_answer"] is True
    assert source["collection_arms"] == {key: "unknown" for key in ("fts", "anchor", "lane_seed", "recent")}
    assert len(source["stages"]) <= 10
    for stage in source["stages"]:
        assert stage["target_presence"] == stage["binding_status"] == "unknown"
        assert stage["observed_count"] is stage["inspected_count"] is stage["window_complete"] is stage["truncated"] is None
        assert stage["source_score"] is stage["source_rank"] is None
        assert PRIVATE not in stage["drop_reasons"]
    serialized = json.dumps(diagnostic)
    assert PRIVATE not in serialized and "private-member" not in serialized and "storage_key" not in serialized
