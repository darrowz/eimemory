from eimemory.scheduler.result_contract import nightly_result_diagnostics


def test_nightly_diagnostics_surface_post_run_recall_smoke_metrics():
    report = {"production_recall": {
        "ok": True, "sample_count": 10, "false_recall_rate": 0.1, "false_recall_count": 1,
        "evaluation_contract": "known_item_smoke.v1", "persisted_record_id": "ref_x",
        "quality_gate": {"ok": False, "blocked_reason": "recall_quality_evidence_incomplete",
                         "thresholds": {"false_recall_rate": 0.05}},
        "samples": [{"query": "private raw query"}],
    }}
    diag = nightly_result_diagnostics(report, [])["production_recall_smoke"]
    assert diag["status"] == "evaluated" and diag["sample_count"] == 10
    assert diag["false_recall_rate"] == 0.1 and diag["false_recall_threshold"] == 0.05
    assert diag["gate_ok"] is False and diag["persisted_record_id"] == "ref_x"
    assert "private raw query" not in repr(diag)


def test_nightly_diagnostics_report_missing_or_skipped_recall_smoke():
    assert nightly_result_diagnostics({}, [])["production_recall_smoke"] == {"status": "not_run"}
    skipped = nightly_result_diagnostics({"production_recall": {"ok": True, "eval_skipped_reason": "no_cases"}}, [])
    assert skipped["production_recall_smoke"] == {"status": "not_evaluated", "reason": "no_cases"}
