"""One bounded detector, routing and recovery owner for nightly and watch."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from eimemory.models.records import ScopeRef


def _run_system_code_maintenance(
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
            "schema": "system_code_maintenance.v1", "report_type": "code_evolution_maintenance",
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
        return {**_not_run_report("system_detection_disabled"), **recovery,
                "code_evolution": recovery, "execution_ok": recovery.get("ok") is True,
                "system_detection_enabled": False, "repair_complete": False}
    from eimemory.ops.runtime_identity_drift import (
        inspect_live_runtime_identity,
        validate_runtime_identity_drift_report,
    )
    from eimemory.ops.system_code_repair_failure import record_system_code_repair_failure
    from eimemory.governance.evolution.system_code_repair import process_system_code_incidents
    from eimemory.governance.evolution.code_automation_policy_owner import ensure_native_incident_policy
    from eimemory.governance.evolution.code_evolution_incident_coverage import (
        checkpoint_native_ingress, observe_native_incident,
    )

    ingress_checkpoint = checkpoint_native_ingress(runtime, scope=scope_ref)
    from eimemory.governance.evolution.managed_code_incident_proof import checkpoint_managed_ingress
    managed_checkpoint=checkpoint_managed_ingress()
    # This native acquisition follows the precise ingress checkpoint. Keeping
    # its actual subsecond instant avoids declaring it before that checkpoint.
    detected_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
        if policy_owner.get("ok") is not True:
            route = {
                "ok": False, "status": "blocked", "reason": "native_policy_owner_unverified", "processed": []}
            failure = {
                "ok": True, "status": "skipped", "reason": "router_not_executed", "incident_record_id": ""}
        elif policy_owner.get("status") == "manual_policy_preserved":
            # Expired/unowned automation policy stays operator-owned. Do not
            # route incidents that need a live automation identity; recovery
            # and failure accounting still run so leases are not stranded.
            route = {
                "ok": True, "status": "deferred", "reason": "manual_policy_preserved", "processed": []}
            failure = _call(
                lambda: record_system_code_repair_failure(
                    runtime, scope=scope_ref, repair_report=route, detected_at=detected_at),
                "repair_failure_detector",
            )
        else:
            route = _call(
                lambda: process_system_code_incidents(
                    runtime, scope=scope_ref, repo_root=repo_root, max_items=1,
                    **({'capability_authority': policy_owner['capability_authority']}
                       if 'capability_authority' in policy_owner else {})),
                "incident_router")
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
        "deferred" if route.get("status") == "deferred" else (
        "processed" if route.get("processed") or recovery.get("resumed_count", 0) else (
            "observed" if detector.get("incident_record_id") or failure.get("incident_record_id") else "idle")))
    observed_ids = list(dict.fromkeys(value for value in
        (detector.get('incident_record_id'), failure.get('incident_record_id')) if value))
    ingress_observations = [observe_native_incident(runtime, scope=scope_ref, record_id=value) for value in observed_ids]
    return {
        "schema": "system_code_maintenance.v1", "report_type": "code_evolution_maintenance",
        "ok": execution_ok, "execution_ok": execution_ok, "status": status,
        "system_detection_enabled": True,
        "repair_complete": False, "detected_at": detected_at,
        "reason": ("manual_policy_preserved" if status == "deferred" else "")
                  if execution_ok else "system_maintenance_incomplete",
        "runtime_identity": detector, "system_code_repair": route,
        "native_policy_owner": policy_owner,
        "managed_knowledge_checkpoint":managed_checkpoint,
        "system_code_repair_failure": failure, "code_evolution": recovery,
        # Coverage diagnostics never replace repair permission, original global
        # provenance, or the product's terminal/8-hour qualification gates.
        "incident_coverage": {"diagnostic_only": True, "checkpoint": ingress_checkpoint,
                              "observations": ingress_observations, "qualifies_for_product_completion": False},
    }


def _skipped() -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "reason": "apply_disabled"}


def _not_run_report(reason: str) -> dict[str, Any]:
    return {
        "schema": "system_code_maintenance.v1", "report_type": "code_evolution_maintenance",
        "ok": False, "execution_ok": False, "status": "blocked", "reason": reason,
        "repair_complete": False, "system_detection_enabled": False,
        **{stage: {"ok": False, "status": "not_run", "reason": reason}
           for stage in ("runtime_identity", "native_policy_owner", "system_code_repair",
                         "system_code_repair_failure", "code_evolution")},
    }


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


_OWNER_AUTHORITY_REASONS = frozenset({
    'code_provider_authority_capability_scope_invalid',
    'code_provider_authority_changed',
    'code_provider_authority_digest_invalid',
    'code_provider_authority_owner_mismatch',
    'code_provider_authority_profile_key_mismatch',
    'code_provider_authority_projection_mismatch',
    'code_provider_authority_snapshot_invalid',
    'code_provider_authority_snapshot_missing',
    'code_provider_delegation_ambiguous',
    'code_provider_delegation_changed_during_read',
    'code_provider_delegation_contract_invalid',
    'code_provider_delegation_entry_invalid',
    'code_provider_delegation_file_not_root_controlled',
    'code_provider_delegation_owner_mismatch',
    'code_provider_delegation_parent_not_root_controlled',
    'code_provider_delegation_path_invalid',
    'code_provider_delegation_profile_mismatch',
    'code_provider_exact_scope_required',
    'code_provider_profile_requires_exact_code_implementation',
    'code_provider_scope_not_authorized',
    'standing_grant_capability_profile_digest_mismatch',
    'standing_grant_changed_during_read',
    'standing_grant_contract_invalid',
    'standing_grant_delegation_profile_mismatch',
    'standing_grant_exact_scope_required',
    'standing_grant_expired_or_not_yet_valid',
    'standing_grant_file_not_root_controlled',
    'standing_grant_incident_not_authorized',
    'standing_grant_missing_or_unsafe',
    'standing_grant_object_required',
    'standing_grant_owner_check_unsupported',
    'standing_grant_parent_not_root_controlled',
    'standing_grant_profile_digest_mismatch',
    'standing_grant_repository_mismatch',
    'standing_grant_revoked_or_killed',
    'standing_grant_route_mismatch',
    'standing_grant_routes_invalid',
    'standing_grant_time_invalid',
    'user_control_plane_account_invalid',
    'user_control_plane_directory_changed',
    'user_control_plane_directory_invalid',
    'user_control_plane_directory_owner_invalid',
    'user_control_plane_directory_untrusted',
    'user_control_plane_file_changed',
    'user_control_plane_file_untrusted',
    'user_control_plane_mode_invalid',
    'user_control_plane_namespace_not_readonly',
    'user_control_plane_namespace_unverified',
    'user_control_plane_native_consumer_unverified',
    'user_control_plane_nonroot_identity_required',
    'user_control_plane_posix_identity_required',
})


def run_system_code_maintenance(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any], owner_id: str,
    limit: int = 100, apply: bool = True, repo_root: Any = None,
    detect_and_route: bool = True, include_registered_owners: bool = False,
) -> dict[str, Any]:
    """Discover registered owners; each owner retains its own standing authority."""
    from dataclasses import asdict
    from pathlib import Path
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    kwargs = dict(owner_id=owner_id, limit=limit, apply=apply,
                  repo_root=repo_root, detect_and_route=detect_and_route)
    if apply is not True or detect_and_route is not True or include_registered_owners is not True:
        return _run_system_code_maintenance(runtime, scope=scope_ref, **kwargs)
    reports = []
    primary = None
    candidates = []
    try:
        from .code_evolution_provider_authority import _secure_delegations
        from .code_automation_policy_owner import _grant, _verify_profile, _resolve_repo_root

        def verify(entry):
            target = ScopeRef.from_dict(entry['owner_scope'])
            grant = _grant(target, Path(_resolve_repo_root(repo_root)), datetime.now(timezone.utc))
            if (grant['profile_key'], grant['profile_digest']) != (entry['profile_key'], entry['profile_digest']):
                raise ValueError('standing_grant_delegation_profile_mismatch')
            _verify_profile(runtime, target, grant)
            return target

        registry = _secure_delegations()
        for entry in (registry or {}).get('delegations', []):
            candidate = entry['owner_scope']
            if (entry['provider_scope'] != asdict(scope_ref) or candidate == asdict(scope_ref)
                    or any(candidate[k] != getattr(scope_ref, k) for k in ('tenant_id', 'agent_id'))):
                continue
            verify(entry)
            candidates.append(entry)
        # Preflight prevents known admission failures before any maintenance.
        # Execution is sequential, not atomic: later failures never undo earlier effects.
        primary = _run_system_code_maintenance(runtime, scope=scope_ref, **kwargs)
        for entry in candidates:
            target = verify(entry)  # Authority can change after preflight or primary execution.
            if primary.get('ok') is not True:
                # Independent authorization observation is safe after failure;
                # maintenance effects still require the primary gate to pass.
                reports.append({'scope': asdict(target), 'report': {
                    **_not_run_report('primary_maintenance_failed'),
                    'standing_authority': {'status': 'verified', 'effects_exercised': False},
                }})
                continue
            report = _run_system_code_maintenance(runtime, scope=target, **kwargs)
            reports.append({'scope': asdict(target), 'report': report})
            if report.get('ok') is not True:
                break
    except Exception as exc:
        reason = 'owner_scope_authority_error:' + type(exc).__name__
        return {**_not_run_report(reason), **(primary or {}),
                'ok': False, 'execution_ok': False, 'status': 'blocked',
                'primary_report': primary, 'owner_scope_reports': reports,
                'owner_routing_reason': reason,
                'owner_authority_reason': str(exc) if isinstance(exc, ValueError) and str(exc) in _OWNER_AUTHORITY_REASONS
                                          else 'authority_check_unclassified'}
    if not reports:
        return primary
    ok = primary.get('ok') is True and all(row['report'].get('ok') is True for row in reports)
    statuses = {report.get('status') for report in [primary, *(row['report'] for row in reports)]}
    status = next((value for value in ('deferred', 'processed', 'observed') if value in statuses),
                  primary.get('status')) if ok else 'blocked'
    return {**primary, 'ok': ok, 'execution_ok': ok, 'primary_report': primary,
            'owner_scope_reports': reports,
            'status': status,
            'reason': 'owner_maintenance_deferred' if status == 'deferred' else primary.get('reason', '')}
