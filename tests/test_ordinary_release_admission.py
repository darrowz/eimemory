"""Ordinary release admission is not the L5 or recall-quality verdict."""
from eimemory.governance.release.closure_verdict import split_release_conclusions


def _receipt_ok(commit="a" * 40):
    return {
        "ok": True,
        "commit": commit,
        "promotion_request_id": "rec-1",
        "release_session_id": "sess-1",
        "release_path": "/opt/eimemory/releases/" + commit,
    }


def _deployment(commit="a" * 40):
    return {
        "commit": commit,
        "promotion_request_id": "rec-1",
        "release_path": "/opt/eimemory/releases/" + commit,
    }


def test_l5_gap_does_not_deny_ordinary_release_or_certify_quality():
    report = {
        "report_type": "l5_release_closure",
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_stage": "readiness",
        "blocked_reason": "bootstrap_pending_non_recall_l5_evidence_incomplete",
        "deployment": _deployment(),
        "deployment_receipt": _receipt_ok(),
        "production_recall_gate": {"ok": False, "status": "data_accumulating", "sample_count": 0},
        "readiness": {"ok": False, "current_stage": "L4"},
    }
    split = split_release_conclusions(report, execution={"closure_exit_status": 1, "expected_commit": "a" * 40})
    assert split["ordinary_release_admission"] == "admitted"
    assert split["l5_certification"] == "incomplete"
    assert split["production_quality"] == "uncertified"
    assert split["historical_pending_blocks_ordinary_release"] is False


def test_receipt_mismatch_and_process_failure_deny_ordinary_release():
    report = {
        "report_type": "l5_release_closure",
        "ok": False,
        "closure_complete": False,
        "blocked_stage": "readiness",
        "blocked_reason": "bootstrap_pending_non_recall_l5_evidence_incomplete",
        "deployment": _deployment(),
        "deployment_receipt": {**_receipt_ok(), "promotion_request_id": "other", "release_path": "/tmp/wrong"},
        "readiness": {"ok": False, "current_stage": "L4"},
    }
    mismatched = split_release_conclusions(report, execution={"closure_exit_status": 1, "expected_commit": "a" * 40})
    crashed = split_release_conclusions(
        {**report, "deployment_receipt": _receipt_ok()},
        execution={"closure_exit_status": 2, "expected_commit": "b" * 40},
    )
    assert mismatched["ordinary_release_admission"] == "denied"
    assert crashed["ordinary_release_admission"] == "denied"


def test_contradictions_do_not_certify_l5_or_quality():
    base = {
        "report_type": "l5_release_closure",
        "ok": True,
        "closure_complete": True,
        "data_accumulating": False,
        "deployment": _deployment(),
        "deployment_receipt": _receipt_ok(),
        "production_recall_gate": {"ok": True, "status": "accepted", "sample_count": 3, "gate_ok": False,
                                   "threshold_gate": {"ok": False}},
        "readiness": {"ok": True, "current_stage": "L5"},
    }
    by_exit = split_release_conclusions(base, execution={"closure_exit_status": 2})
    by_commit = split_release_conclusions(base, execution={"expected_commit": "b" * 40})
    by_hard = split_release_conclusions(base, hard_errors=[{"code": "closure_report_release_mismatch"}])
    by_gate = split_release_conclusions(base)
    assert by_exit["l5_certification"] != "certified"
    assert by_commit["production_quality"] != "certified"
    assert by_hard["l5_certification"] != "certified"
    assert by_gate["production_quality"] != "certified"
    assert by_gate["l5_certification"] != "certified"
    report = {
        "report_type": "l5_release_closure",
        "ok": True,
        "closure_complete": True,
        "data_accumulating": True,
        "deployment": _deployment(),
        "deployment_receipt": _receipt_ok(),
        "production_recall_gate": {"ok": True, "status": "accepted", "sample_count": 0},
        "readiness": {"ok": False, "current_stage": "L5"},
    }
    split = split_release_conclusions(report)
    assert split["l5_certification"] != "certified"
    assert split["production_quality"] != "certified"


def test_deployment_failure_still_denies_ordinary_release():
    report = {
        "report_type": "l5_release_closure",
        "ok": False,
        "closure_complete": False,
        "blocked_stage": "deployment_receipt",
        "blocked_reason": "bootstrap_pending_non_recall_l5_evidence_incomplete",
        "deployment": {"commit": "a" * 40, "promotion_request_id": "rec-1"},
        "deployment_receipt": {"ok": False, "commit": "a" * 40},
        "readiness": {"ok": False, "current_stage": "L4"},
    }
    split = split_release_conclusions(report)
    assert split["ordinary_release_admission"] == "denied"
    assert split["l5_certification"] != "certified"
    assert split["production_quality"] != "certified"
