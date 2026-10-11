"""Missing evidence diagnostics preserve unknowns and exclude private details."""
from copy import deepcopy
import json

from eimemory.scheduler.result_contract import nightly_result_diagnostics, _dynamic_failure_diagnostics


def test_dynamic_result_count_does_not_invent_candidate_counts():
    report = {"execution": {"results": [
        {"status": "blocked", "reason": "hypothesis_missing_or_ambiguous"},
        {"status": "blocked", "reason": "hypothesis_missing_or_ambiguous", "candidate_hypothesis_count": 0},
        {"status": "blocked", "reason": "hypothesis_missing_or_ambiguous", "candidate_hypothesis_count": 2},
        {"status": "blocked", "reason": "hypothesis_missing_or_ambiguous", "candidate_hypothesis_count": False},
    ]}}
    snapshot = deepcopy(report)
    result = _dynamic_failure_diagnostics(report)
    assert result["result_count"] == 4
    assert result["candidate_hypothesis_counts"] == [None, 0, 2, None]
    assert result["candidate_hypothesis_counts_reported"] == 2
    assert result["reason_counts"] == {"hypothesis_missing_or_ambiguous": 4}
    assert report == snapshot


def test_memory_and_hypothesis_reasons_reach_nightly_summary_without_private_content():
    report = {
        "memory_eval_ci": {"ok": True, "memory_benchmark_status": "not_run", "retrieval_case_count": 0,
                           "dataset_source": "replay_dataset", "eval_skipped_reason": "memory_eval_dataset_empty",
                           "blocked_reason": "memory_eval_dataset_empty", "detail": "private-token"},
        "capability_hypothesis_producer": {"ok": True, "status": "no_eligible_evidence", "skipped": [
            {"reason": "no_applicable_knowledge_link_for_gap_revision", "error": "private-token"},
            {"reason": "ambiguous_applicable_knowledge_links"}, {"reason": "private-token"}]},
        "production_recall_auto_review": {"ok": True, "review_contract": "production_recall_review_conclusion.v1",
                                         "reviewed_count": 5, "passed_count": 1, "not_passed_count": 4,
                                         "pending_count": 0, "open_review_count": 0,
                                         "reason_counts": {"not_passed": {"tool_free_transport_unavailable": 4,
                                                                         "private-token": 1}},
                                         "review_results": [{"private": "private-token"}]},
    }
    result = nightly_result_diagnostics(report, [])
    assert result["memory_benchmark"] == {
        "status": "not_run", "dataset_source": "replay_dataset", "retrieval_case_count": 0,
        "eval_skipped_reason": "memory_eval_dataset_empty", "blocked_reason": "memory_eval_dataset_empty"}
    assert result["capability_hypothesis_producer"]["skipped_reason_counts"] == {
        "no_applicable_knowledge_link_for_gap_revision": 1, "ambiguous_applicable_knowledge_links": 1,
        "reason_not_allowlisted": 1}
    review = result["recall_label_auto_review"]
    assert (review["reviewed_count"], review["passed_count"], review["not_passed_count"]) == (5, 1, 4)
    assert review["pending_count"] == review["open_review_count"] == 0
    assert review["not_passed_reason_counts"] == {"tool_free_transport_unavailable": 4,
                                                "reason_not_allowlisted": 1}
    assert result["memory_benchmark_accepted"] is False
    assert "private-token" not in json.dumps(result)


def test_missing_legacy_diagnostics_remain_unknown():
    result = nightly_result_diagnostics({"memory_eval_ci": {"ok": True},
                                        "production_recall_auto_review": {"ok": True},
                                        "capability_hypothesis_producer": {"ok": True}}, [])
    assert result["memory_benchmark"]["retrieval_case_count"] is None
    assert result["memory_benchmark"]["blocked_reason"] == "not_reported"
    assert result["capability_hypothesis_producer"]["skipped_count"] is None
    assert result["recall_label_auto_review"]["not_passed_count"] is None
    assert result["recall_label_auto_review"]["not_passed_reason_counts"] is None
    assert result["recall_label_auto_review"]["not_passed_reasons_truncated"] is None


def test_reason_projection_discloses_truncation_and_keeps_unknown_text_private():
    reasons = {f"private_reason_{index}": 1 for index in range(101)}
    report = {"production_recall_auto_review": {"reason_counts": {"not_passed": reasons}}}
    result = nightly_result_diagnostics(report, [])["recall_label_auto_review"]
    assert result["not_passed_reasons_truncated"] is True
    assert result["not_passed_reason_counts"] == {"reason_not_allowlisted": 100}
    assert "private_reason" not in json.dumps(result)
