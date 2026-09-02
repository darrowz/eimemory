"""Detect and persist a stale-policy failure in the system repair router."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    load_code_automation_policy,
)
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.models.records import RecordEnvelope, ScopeRef


DETECTOR_ID = "eimemory.system_code_repair_failure.v1"
INCIDENT_CLASS = "code.system_repair_policy_stale"
SOURCE = "eimemory.system_code_repair_failure"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def detect_system_code_repair_failure(
    repair_report: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    release_commit: str,
    detected_at: str,
) -> dict[str, Any]:
    """Turn an observed stale consumed policy into one bounded incident."""

    current_commit = str(release_commit or "").strip().lower()
    repository = policy.get("repository") if isinstance(policy.get("repository"), Mapping) else {}
    policy_base_commit = str(repository.get("base_commit") or "").strip().lower()
    policy_digest = str(policy.get("policy_digest") or "").strip().lower()
    policy_transaction_id = str(repair_report.get("policy_transaction_id") or "").strip()
    actionable = bool(
        repair_report.get("ok") is False
        and repair_report.get("status") == "blocked"
        and repair_report.get("reason") == "automation_policy_already_consumed"
        and policy.get("ok") is True
        and policy.get("status") == "enabled"
        and _COMMIT_RE.fullmatch(current_commit)
        and _COMMIT_RE.fullmatch(policy_base_commit)
        and policy_base_commit != current_commit
        and _DIGEST_RE.fullmatch(policy_digest)
        and policy_transaction_id
    )
    observation = {
        "schema": "system_code_repair_failure.v1",
        "detector": DETECTOR_ID,
        "detected_at": str(detected_at or ""),
        "release_commit": current_commit,
        "blocked_reason": str(repair_report.get("reason") or ""),
        "consumed_policy_digest": policy_digest,
        "consumed_policy_base_commit": policy_base_commit,
        "policy_transaction_id": policy_transaction_id,
    }
    incident: dict[str, Any] | None = None
    if actionable:
        identity = {key: value for key, value in observation.items() if key != "detected_at"}
        digest = _digest(identity)
        incident = {
            "incident_id": f"incident-system-repair-policy-{digest[:24]}",
            "incident_digest": digest,
            "incident_class": INCIDENT_CLASS,
            "title": "Consumed stale policy blocks current system repair routing",
            "summary": (
                f"The current release {current_commit} observed that the consumed one-shot policy "
                f"for older base {policy_base_commit} stops the autonomous repair router before it "
                "can establish whether any current incident matches that policy. Make stale policy "
                "state idle when it authorizes no current trusted incident, without reusing or "
                "resetting the consumed grant."
            ),
            "diagnostic_codes": ["system_code_repair:automation_policy_already_consumed"],
            "acceptance_requirements": [
                "consumed_policy_is_never_reused_or_reset",
                "policy_consumption_checked_only_for_an_exact_current_incident",
                "stale_policy_without_matching_current_incident_returns_idle",
                "provider_not_called_without_exact_unconsumed_policy",
                "protected_routing_regression_plan_passes",
            ],
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


def record_system_code_repair_failure(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    repair_report: Mapping[str, Any],
    detected_at: str = "",
) -> dict[str, Any]:
    """Persist one idempotent detector incident from an actual router report."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    release = current_release_identity(runtime, scope_ref)
    policy = load_code_automation_policy(path=CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    report = detect_system_code_repair_failure(
        repair_report,
        policy=policy,
        release_commit=release.commit if release is not None else "",
        detected_at=detected_at or now_iso(),
    )
    incident = report.get("incident")
    if not isinstance(incident, Mapping):
        return {**report, "incident_record_id": ""}
    marker = f"system-code-repair-failure:{incident['incident_digest']}"
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
                detail="Bounded system observation from the autonomous repair router.",
                content={**dict(incident), "detector_report": report},
                tags=["code-evolution", "system-repair", "system-detected"],
                source=SOURCE,
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
    "SOURCE",
    "detect_system_code_repair_failure",
    "record_system_code_repair_failure",
]
