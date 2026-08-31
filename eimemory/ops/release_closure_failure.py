"""Detect and persist actionable failures in the release-closure controller."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


DETECTOR_ID = "eimemory.release_closure_failure.v1"
INCIDENT_CLASS = "release.closure_internal_failure"
_NON_ACTIONABLE_REASONS = frozenset(
    {
        "current_release_channel_receipt_not_found",
        "production_dataset_not_ready",
        "production_recall_dataset_empty",
        "production_recall_dataset_unconfigured",
        "eligible_dataset_missing",
        "strict_code_evolution_receipt_required",
        "observation_not_valid",
        "waiting_for_observation",
    }
)


def detect_release_closure_failure(
    closure_report: Mapping[str, Any],
    *,
    detected_at: str,
) -> dict[str, Any]:
    if not isinstance(closure_report, Mapping):
        raise ValueError("closure_report must be a mapping")
    stage = str(closure_report.get("blocked_stage") or "").strip()
    reason = str(closure_report.get("blocked_reason") or "").strip()
    deployment = closure_report.get("deployment")
    deployment = deployment if isinstance(deployment, Mapping) else {}
    lineage = closure_report.get("release_lineage")
    lineage = lineage if isinstance(lineage, Mapping) else {}
    unknown_paths = lineage.get("unknown_production_paths")
    unknown_paths = unknown_paths if isinstance(unknown_paths, (list, tuple)) else []
    commit = str(deployment.get("commit") or "").strip().lower()
    version = str(deployment.get("version") or "").strip()
    actionable = bool(
        closure_report.get("ok") is not True
        and closure_report.get("data_accumulating") is not True
        and stage
        and reason
        and reason not in _NON_ACTIONABLE_REASONS
    )
    observation = {
        "schema": "release_closure_failure.v1",
        "detector": DETECTOR_ID,
        "detected_at": str(detected_at or ""),
        "release_commit": commit,
        "release_version": version,
        "blocked_stage": stage,
        "blocked_reason": reason,
        "lineage_compatible": lineage.get("compatible") is True,
        "unknown_production_paths": sorted(
            str(item)[:512]
            for item in unknown_paths[:50]
            if str(item).strip()
        ),
    }
    incident: dict[str, Any] | None = None
    if actionable:
        identity = {
            key: observation[key]
            for key in (
                "schema",
                "detector",
                "release_commit",
                "blocked_stage",
                "blocked_reason",
                "lineage_compatible",
                "unknown_production_paths",
            )
        }
        digest = _digest(identity)
        acceptance_requirements = [
            "focused_failure_reproduction",
            "protected_regression_tests_pass",
            "evidence_gates_not_weakened",
            "current_release_lineage_compatible",
        ]
        if reason == "code_evolution_gate_evidence_missing":
            acceptance_requirements.extend(
                [
                    "code_evolution_gate_uses_exact_current_deployment_receipt",
                    "deployment_receipt_is_authoritative_input",
                    "storage_acceptance_records_are_not_deployment_receipts",
                    "deployment_receipt_fallback_is_forbidden",
                    "missing_deployment_receipt_fails_closed",
                ]
            )
        incident = {
            "incident_id": f"incident-release-closure-{digest[:24]}",
            "incident_digest": digest,
            "incident_class": INCIDENT_CLASS,
            "title": "Release closure failed after technical health passed",
            "summary": (
                f"The production release {commit or 'unknown'} passed technical deployment "
                f"but release closure stopped at {stage}: {reason}. Diagnose and correct the "
                "bounded closure or lineage implementation without weakening evidence gates."
                + (
                    " The deployment receipt is an authoritative input independent from storage "
                    "acceptance records; never infer or replace it from live record IDs."
                    if reason == "code_evolution_gate_evidence_missing"
                    else ""
                )
            ),
            "diagnostic_codes": [f"{stage}:{reason}"],
            "acceptance_requirements": acceptance_requirements,
        }
    return {
        **observation,
        "ok": not actionable,
        "status": "failure_detected" if actionable else "non_actionable",
        "origin": "system_detector",
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "observation_valid": True,
        "incident": incident,
    }


def record_release_closure_failure(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    closure_report: Mapping[str, Any],
    detected_at: str,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    report = detect_release_closure_failure(closure_report, detected_at=detected_at)
    incident = report.get("incident")
    if not isinstance(incident, Mapping):
        return {**report, "incident_record_id": ""}
    marker = f"release-closure-failure:{incident['incident_digest']}"
    existing = runtime.store.list_records_by_meta_value(
        kinds=["incident"],
        scope=scope_ref,
        meta_key="idempotency_key",
        meta_value=marker,
        limit=1,
    )
    if existing:
        record = existing[0]
    else:
        record = runtime.store.append(
            RecordEnvelope.create(
                kind="incident",
                title=str(incident["title"]),
                summary=str(incident["summary"]),
                detail="Bounded system observation from the post-deploy release-closure controller.",
                content={**dict(incident), "detector_report": report},
                tags=["code-evolution", "release-closure", "system-detected"],
                source="eimemory.release_closure_failure",
                scope=scope_ref,
                status="active",
                provenance={
                    "origin": "system_detector",
                    "detector": DETECTOR_ID,
                    "known_before_detection": False,
                    "prior_user_reported": False,
                },
                meta={
                    "idempotency_key": marker,
                    "incident_class": INCIDENT_CLASS,
                    "incident_digest": incident["incident_digest"],
                    "observation_valid": True,
                },
            )
        )
    return {**report, "incident_record_id": record.record_id}


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DETECTOR_ID",
    "INCIDENT_CLASS",
    "detect_release_closure_failure",
    "record_release_closure_failure",
]
