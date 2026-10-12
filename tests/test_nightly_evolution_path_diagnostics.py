"""Evolution summaries retain source-bound conclusions without private inputs."""
from copy import deepcopy
from dataclasses import asdict
import json

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.result_contract import nightly_result_diagnostics


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
