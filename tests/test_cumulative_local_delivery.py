from eimemory.retrieval.stage_diagnostics import retrieval_stage_diagnostics


def test_huge_diagnostic_counts_do_not_overflow():
    result = retrieval_stage_diagnostics({"engine_diagnostics": {"candidate_count": 10**400}})
    assert result["engine"]["candidate_count"] == 1000000


def test_delivery_claims_require_observed_local_argument():
    explanation = {"delivery_diagnostics": {"status": "context_delivered", "delivered_count": 99}}
    assert retrieval_stage_diagnostics(explanation, trusted_retrieval=False)["delivery"] == {}
    local = {"status": "no_context", "delivered_count": 0}
    assert retrieval_stage_diagnostics(explanation, local_delivery=local)["delivery"] == local
