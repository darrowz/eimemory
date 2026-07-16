from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import ScopeRef


def run_release_closure(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
) -> dict[str, Any]:
    scope_payload = asdict(scope) if isinstance(scope, ScopeRef) else dict(scope or {})
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    report: dict[str, Any] = {
        "ok": False,
        "closure_complete": False,
        "report_type": "l5_release_closure",
        "scope": scope_payload,
        "blocked_stage": "",
        "blocked_reason": "",
        "deployment": {},
        "record_ids": {},
        "deployment_receipt": dict(not_run),
        "live_acceptance": dict(not_run),
        "closure_rehearsal": dict(not_run),
        "readiness": dict(not_run),
    }
    identity_kwargs = {
        "scope": scope_payload,
        "repo_root": str(repo_root),
        "current_link": str(current_link),
        "health_url": str(health_url),
        "prior_commit": str(prior_commit),
    }

    receipt = runtime.verify_and_record_deployment(**identity_kwargs)
    report["deployment_receipt"] = receipt
    if receipt.get("ok") is not True:
        return _blocked(report, "deployment_receipt", _failure_reason(receipt, "deployment_receipt_failed"))
    report["deployment"] = _deployment_identity(receipt)
    report["record_ids"]["deployment_receipt"] = str(receipt.get("promotion_request_id") or "")

    live_acceptance = runtime.run_live_task_acceptance(**identity_kwargs)
    report["live_acceptance"] = live_acceptance
    if not _live_acceptance_ok(live_acceptance, receipt=receipt):
        return _blocked(report, "live_acceptance", _failure_reason(live_acceptance, "live_acceptance_failed"))

    rehearsal = runtime.run_l5_closure_rehearsal(scope=scope_payload, persist=True)
    report["closure_rehearsal"] = rehearsal
    if rehearsal.get("ok") is not True or rehearsal.get("closure_complete") is not True:
        return _blocked(report, "closure_rehearsal", _failure_reason(rehearsal, "closure_rehearsal_failed"))

    readiness = runtime.build_l5_readiness_report(
        scope=scope_payload,
        persist=True,
        limit=1000,
        loop_id="release_closure",
    )
    report["readiness"] = readiness
    report["record_ids"]["readiness"] = str(readiness.get("persisted_record_id") or "")
    if not _readiness_ok(readiness):
        return _blocked(report, "readiness", "readiness_not_l5")

    report["ok"] = True
    report["closure_complete"] = True
    return report


def _blocked(report: dict[str, Any], stage: str, reason: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_stage"] = str(stage)
    report["blocked_reason"] = str(reason)
    return report


def _failure_reason(stage_report: dict[str, Any], fallback: str) -> str:
    error = str(stage_report.get("error") or stage_report.get("blocked_reason") or "").strip()
    if error:
        return error
    blocked = [str(item).strip() for item in stage_report.get("blocked_reasons") or [] if str(item).strip()]
    return blocked[0] if blocked else fallback


def _deployment_identity(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "commit": str(receipt.get("commit") or ""),
        "version": str(receipt.get("version") or ""),
        "release_path": str(receipt.get("release_path") or ""),
        "promotion_request_id": str(receipt.get("promotion_request_id") or ""),
    }


def _live_acceptance_ok(report: dict[str, Any], *, receipt: dict[str, Any]) -> bool:
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    return bool(
        report.get("ok") is True
        and int(report.get("case_count") or 0) == 10
        and int(report.get("pass_count") or 0) == 10
        and int(report.get("fail_count") or 0) == 0
        and int(report.get("distinct_task_types") or 0) == 10
        and deployment == _deployment_identity(receipt)
    )


def _readiness_ok(readiness: dict[str, Any]) -> bool:
    score = readiness.get("readiness_score")
    assessment = readiness.get("latest_l5_assessment") if isinstance(readiness.get("latest_l5_assessment"), dict) else {}
    live_gate = readiness.get("live_task_gate") if isinstance(readiness.get("live_task_gate"), dict) else {}
    replay = readiness.get("verified_replay") if isinstance(readiness.get("verified_replay"), dict) else {}
    return bool(
        readiness.get("ok") is True
        and readiness.get("current_stage") == "L5"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 1.0
        and assessment.get("complete") is True
        and live_gate.get("ok") is True
        and int(live_gate.get("current_deployment_acceptance") or 0) >= 10
        and not list(replay.get("weak_capabilities_missing") or [])
    )
