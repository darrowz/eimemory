"""Repairable closure-report projection, never release or novelty authority.

The protected verdict recomputes its expected typed projection independently.
A faulty leaf cannot change admission, native fault codes or source facts.
"""
from __future__ import annotations
from typing import Any
from eimemory.governance.release.closure_contracts import acceptance_failure_details,channel_wait_report_ok

def reported_release_summary(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("release closure report must be an object")
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    recall_gate = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    recall_threshold = (
        recall_gate.get("threshold_gate")
        if isinstance(recall_gate.get("threshold_gate"), dict)
        else {}
    )
    if "gate_ok" in recall_gate:
        recall_gate_ok = recall_gate.get("gate_ok") is True
    elif recall_threshold:
        recall_gate_ok = recall_threshold.get("ok") is True
    else:
        recall_gate_ok = recall_gate.get("ok") is True
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    channel = (
        report.get("channel_acceptance")
        if isinstance(report.get("channel_acceptance"), dict)
        else {}
    )
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    rehearsal_complete = rehearsal.get("closure_complete") is True
    rehearsal_accumulating = rehearsal.get("data_accumulating") is True
    closure_complete = report.get("closure_complete") is True
    data_accumulating = report.get("data_accumulating") is True
    if (
        report.get("ok") is True
        and report.get("report_type") == "code_evolution_pre_observation"
        and report.get("status") == "ready_for_observation"
        and not closure_complete
        and not data_accumulating
    ):
        business_closure_outcome = "ready_for_observation"
    elif report.get("ok") is True and closure_complete and not data_accumulating:
        business_closure_outcome = "closure_complete"
    elif report.get("ok") is True and data_accumulating and not closure_complete:
        business_closure_outcome = "data_accumulating"
    elif channel_wait_report_ok(report):
        # This is a bound wait for external evidence, never closure success.
        business_closure_outcome = "data_accumulating"
        data_accumulating = True
    else:
        business_closure_outcome = "failed"
    return {
        "ok": report.get("ok") is True,
        "business_closure_outcome": business_closure_outcome,
        "report_type": str(report.get("report_type") or ""),
        "observation_admission_status": str(report.get("status") or "") if report.get("report_type") == "code_evolution_pre_observation" else "",
        "closure_complete": closure_complete,
        "data_accumulating": data_accumulating,
        "blocked_stage": str(report.get("blocked_stage") or ""),
        "blocked_reason": str(report.get("blocked_reason") or ""),
        "commit": str(deployment.get("commit") or ""),
        "version": str(deployment.get("version") or ""),
        "receipt_id": str(deployment.get("promotion_request_id") or ""),
        "production_recall_gate_ok": recall_gate_ok,
        "production_recall_gate_status": str(
            recall_gate.get("status") or recall_gate.get("gate_status") or ""
        ),
        "production_recall_gate_report_id": str(
            recall_gate.get("report_id")
            or recall_gate.get("record_id")
            or recall_gate.get("persisted_record_id")
            or ""
        ),
        "production_recall_gate_reason": str(
            recall_gate.get("reason") or recall_gate.get("blocked_reason") or ""
        ),
        "replay_ok": replay.get("ok") is True,
        "acceptance_failure": (
            acceptance_failure_details(replay.get("capability_acceptance"))
            if replay.get("ok") is not True and report.get("blocked_stage") == "replay_bootstrap"
            else {}
        ),
        "live_acceptance_ok": live.get("ok") is True,
        "live_pass_count": live.get("pass_count") if type(live.get("pass_count")) is int else 0,
        "live_case_count": live.get("case_count") if type(live.get("case_count")) is int else 0,
        "channel_acceptance_ok": channel.get("ok") is True,
        "channel_acceptance_record_id": str(channel.get("record_id") or ""),
        "rehearsal_ok": rehearsal.get("ok") is True and rehearsal_complete != rehearsal_accumulating,
        "readiness_stage": str(readiness.get("current_stage") or readiness.get("status") or ""),
        "readiness_score": readiness.get("readiness_score"),
    }

