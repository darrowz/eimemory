"""One bounded detector, routing and recovery owner for nightly and watch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


def run_system_code_maintenance(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    owner_id: str,
    limit: int = 100,
    apply: bool = True,
    repo_root: Any = None,
    detect_and_route: bool = True,
) -> dict[str, Any]:
    """Observe real detectors, route at most one incident, and resume leases.

    Submission is never a terminal receipt. Recovery remains available when a
    detector or proposal fails, so already-authorized rollback is not stranded.
    A dry run invokes none of these mutating owners.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
        raise ValueError("maintenance limit must be an integer from 0 to 100")
    if apply is not True:
        return {
            "ok": True, "execution_ok": True, "status": "skipped",
            "reason": "apply_disabled", "repair_complete": False,
            "runtime_identity": _skipped(), "system_code_repair": _skipped(),
            "native_policy_owner": _skipped(),
            "system_code_repair_failure": _skipped(), "code_evolution": _skipped(),
        }
    if detect_and_route is not True:
        recovery = _call(
            lambda: runtime.resume_code_evolution_transactions(scope=scope_ref, owner_id=owner_id, limit=limit),
            "transaction_recovery",
        )
        return {**recovery, "execution_ok": recovery.get("ok") is True,
                "system_detection_enabled": False, "repair_complete": False}
    from eimemory.ops.runtime_identity_drift import (
        inspect_live_runtime_identity,
        validate_runtime_identity_drift_report,
    )
    from eimemory.ops.system_code_repair_failure import record_system_code_repair_failure
    from eimemory.governance.evolution.system_code_repair import process_system_code_incidents
    from eimemory.governance.evolution.code_automation_policy_owner import ensure_native_incident_policy

    detected_at = now_iso()
    detector = _call(
        lambda: inspect_live_runtime_identity(runtime, scope=scope_ref, detected_at=detected_at),
        "runtime_identity_detector",
    )
    detector_valid = bool(
        detector.get("schema") == "runtime_identity_drift.v2"
        and validate_runtime_identity_drift_report(detector, scope=scope_ref)
        and not detector.get("error")
        and ((detector.get("status") == "current" and detector.get("ok") is True)
             or (detector.get("status") == "drift_detected" and detector.get("ok") is False
                 and isinstance(detector.get("incident"), Mapping) and detector.get("incident_record_id")))
    )
    if detector_valid:
        policy_owner = _call(
            lambda: ensure_native_incident_policy(runtime, scope=scope_ref, repo_root=repo_root),
            "native_policy_owner",
        )
        route = (_call(
            lambda: process_system_code_incidents(runtime, scope=scope_ref, repo_root=repo_root, max_items=1,
                **({'capability_authority': policy_owner['capability_authority']}
                   if 'capability_authority' in policy_owner else {})),
            "incident_router") if policy_owner.get("ok") is True else {
                "ok": False, "status": "blocked", "reason": "native_policy_owner_unverified", "processed": []})
        failure = _call(
            lambda: record_system_code_repair_failure(
                runtime, scope=scope_ref, repair_report=route, detected_at=detected_at),
            "repair_failure_detector",
        )
    else:
        policy_owner = {"ok": True, "status": "skipped", "reason": "runtime_identity_unverified"}
        route = {"ok": False, "status": "blocked", "reason": "runtime_identity_unverified", "processed": []}
        failure = {"ok": True, "status": "skipped", "reason": "router_not_executed", "incident_record_id": ""}
    recovery = _call(
        lambda: runtime.resume_code_evolution_transactions(
            scope=scope_ref, owner_id=owner_id, limit=limit),
        "transaction_recovery",
    )
    # A detected drift/failure is an observed fact, not a detector exception.
    # The router and transaction owner retain the policy and terminal verdicts.
    failure_valid = bool(
        failure.get("schema") == "system_code_repair_failure.v1" and not failure.get("error")
        and ((failure.get("status") == "non_actionable" and failure.get("ok") is True)
             or (failure.get("status") == "failure_detected" and failure.get("ok") is False
                 and isinstance(failure.get("incident"), Mapping) and failure.get("incident_record_id")))
    )
    execution_ok = bool(detector_valid and policy_owner.get("ok") is True and route.get("ok") is True
                        and failure_valid and recovery.get("ok") is True)
    status = "blocked" if not execution_ok else (
        "processed" if route.get("processed") or recovery.get("resumed_count", 0) else (
            "observed" if detector.get("incident_record_id") or failure.get("incident_record_id") else "idle"))
    return {
        "schema": "system_code_maintenance.v1", "report_type": "code_evolution_maintenance",
        "ok": execution_ok, "execution_ok": execution_ok, "status": status,
        "system_detection_enabled": True,
        "repair_complete": False, "detected_at": detected_at,
        "reason": "" if execution_ok else "system_maintenance_incomplete",
        "runtime_identity": detector, "system_code_repair": route,
        "native_policy_owner": policy_owner,
        "system_code_repair_failure": failure, "code_evolution": recovery,
    }


def _skipped() -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "reason": "apply_disabled"}


def _call(callback: Any, stage: str) -> dict[str, Any]:
    try:
        value = callback()
        if not isinstance(value, Mapping):
            raise TypeError("maintenance result must be a mapping")
        report = dict(value)
        if (("error" in report and report["error"] not in (None, ""))
                or ("errors" in report and report["errors"] not in (None, []))):
            return {**report, "ok": False, "status": "blocked", "reason": f"{stage}_reported_error"}
        return report
    except Exception as exc:
        return {"ok": False, "status": "blocked", "reason": f"{stage}_error:{type(exc).__name__}"}
