"""System detector for stale release identities in Python runtime units."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
import re
import shlex
import subprocess
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


DETECTOR_ID = "eimemory.runtime_identity_drift.v1"
INCIDENT_CLASS = "deployment.runtime_commit_drift"
PYTHON_RUNTIME_UNITS = (
    "eimemory-real-effect.service",
    "eimemory-audit-verify.service",
    "eimemory-code-implementation-refresh.service",
    "eimemory-console.service",
    "eimemory-learn-dashboard.service",
    "eimemory-learn-think.service",
    "eimemory-learn-watch.service",
    "eimemory-nightly.service",
    "eimemory-rpc.service",
    "eimemory-timer-monitor.service",
    "openclaw-loop-compact.service",
    "openclaw-loop-watch.service",
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
REPORT_SCHEMA = "runtime_identity_drift.v2"
IDENTITY_SCHEMA = "runtime_identity_drift_identity.v2"
CURRENT_SCHEMA = "runtime_identity_drift_current.v1"
CURRENT_SOURCE = "eimemory.runtime_identity_drift.observation"
_SOURCE = "eimemory.runtime_identity_drift"


def detect_runtime_identity_drift(
    *,
    expected_commit: str,
    unit_environments: Mapping[str, str],
    detected_at: str,
    scope: ScopeRef | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare effective systemd environments with one release identity."""
    from eimemory.contracts.code_incident_provenance import native_detection_provenance
    source_facts = native_detection_provenance(DETECTOR_ID)

    commit = str(expected_commit or "").strip().lower()
    if _HEX40.fullmatch(commit) is None:
        raise ValueError("expected_commit must be a full Git SHA")
    if not isinstance(unit_environments, Mapping) or not unit_environments:
        raise ValueError("unit_environments must be a non-empty mapping")
    scope_ref = _exact_scope(scope) if scope is not None else None
    if scope_ref is not None:
        _observation_time(detected_at)
    mismatches: list[dict[str, str]] = []
    for unit in sorted(str(name) for name in unit_environments):
        environment = str(unit_environments.get(unit) or "")
        try:
            assignments = shlex.split(environment, posix=True)
        except ValueError:
            assignments = []
        values = [
            item.split("=", 1)[1]
            for item in assignments
            if "=" in item and item.partition("=")[0] == "EIMEMORY_RUNTIME_COMMIT"
        ]
        if scope_ref is not None:
            values = [value.strip().lower() for value in values]
        if not values:
            mismatches.append(
                {"unit": unit, "reason": "runtime_commit_missing", "observed_commit": ""}
            )
        elif len(values) != 1:
            mismatches.append(
                {"unit": unit, "reason": "runtime_commit_ambiguous", "observed_commit": ""}
            )
        elif values[0] != commit:
            mismatches.append(
                {"unit": unit, "reason": "commit_mismatch", "observed_commit": values[0]}
            )
    material = {
        "schema": "runtime_identity_drift.v1",
        "detector": DETECTOR_ID,
        "expected_commit": commit,
        "detected_at": str(detected_at or ""),
        "mismatches": mismatches,
    }
    if scope_ref is not None:
        identity = {
            "schema": IDENTITY_SCHEMA,
            "detector": DETECTOR_ID,
            "expected_commit": commit,
            "scope": asdict(scope_ref),
            "mismatches": mismatches,
            'source_facts_digest': source_facts['digest'],
        }
        material.update(schema=REPORT_SCHEMA, scope=asdict(scope_ref),
                        identity=identity, identity_digest=_digest(identity))
    incident: dict[str, Any] | None = None
    if mismatches:
        digest = material["identity_digest"] if scope_ref is not None else _digest(material)
        units = ", ".join(item["unit"] for item in mismatches)
        incident = {
            "incident_id": f"incident-runtime-identity-{'v2-' if scope_ref is not None else ''}{digest[:24]}",
            "incident_digest": digest,
            "incident_class": INCIDENT_CLASS,
            "title": "Python runtime units are not bound to the current release",
            "summary": (
                f"Effective EIMEMORY_RUNTIME_COMMIT drift was detected in {units}. "
                "Update the installer's bounded runtime identity policy to select a "
                "final-authority managed drop-in that cannot be overridden by legacy "
                "numeric drop-ins, and verify every discovered Python runtime unit "
                "before issuing a deployment receipt."
            ),
            "diagnostic_codes": sorted({item["reason"] for item in mismatches}),
            "acceptance_requirements": [
                "final_authority_runtime_dropin",
                "all_discovered_python_units_verified",
                "deployment_regression_test",
            ],
        }
    return {
        **material,
        "ok": not mismatches,
        "status": "current" if not mismatches else "drift_detected",
        "origin": "system_detector",
        **source_facts['facts'], 'source_facts': source_facts,
        "observation_valid": True,
        "incident": incident,
    }


def inspect_live_runtime_identity(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    detected_at: str,
    runner: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Read live systemd authority and persist one idempotent system incident."""

    from eimemory.governance.evidence_contract import current_release_identity

    scope_ref = _exact_scope(scope)
    release = current_release_identity(runtime, scope_ref)
    if release is None:
        return {"ok": False, "status": "unavailable", "reason": "current_release_identity_unavailable"}
    read_environment = runner or _systemd_environment
    # Static compatibility lists can contain retired or optional units.  A
    # unit that systemd does not have loaded is not a running Python authority
    # and must not manufacture a permanent runtime-drift incident.  Explicit
    # empty strings from injected runners still mean a loaded unit is missing
    # its commit, preserving the detector's fail-closed contract.
    environments = {
        unit: environment
        for unit in PYTHON_RUNTIME_UNITS
        if (environment := read_environment(unit)) is not None
    }
    if not environments:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no_loaded_python_runtime_units",
        }
    report = detect_runtime_identity_drift(
        expected_commit=release.commit,
        unit_environments=environments,
        detected_at=detected_at,
        scope=scope_ref,
    )
    commit = report["expected_commit"]
    current_id = runtime_identity_current_record_id(scope_ref, commit)

    def persist(sqlite: Any) -> tuple[dict[str, Any], list[RecordEnvelope], list[Any]]:
        # Native BEGIN IMMEDIATE serializes independent watch/nightly processes.
        # Incident first-observation bytes are immutable; only this separate
        # current observation advances when another real system read completes.
        previous = sqlite.get_by_id(current_id, scope=scope_ref, exact_scope=True)
        if previous is not None:
            physical = sqlite.list_records_by_sources_exact_scope(
                scope=scope_ref, sources=[CURRENT_SOURCE], kinds=["reflection"],
                record_ids=[current_id], limit=2)
            if len(physical) != 1 or physical[0].to_dict() != previous.to_dict():
                raise ValueError("runtime_identity_current_observation_conflict")
            previous_report = _current_report(previous, scope_ref, commit)
            if previous_report is None:
                raise ValueError("runtime_identity_current_observation_conflict")
            previous_time = _observation_time(previous_report["detected_at"])
            observed_time = _observation_time(report["detected_at"])
            if observed_time < previous_time:
                if previous_report["identity"] == report["identity"]:
                    incident = previous_report["incident"]
                    return ({**previous_report, "incident_record_id": incident["incident_id"] if incident else "",
                             "current_observation_record_id": current_id}, [], [])
                return ({"ok": False, "status": "unavailable",
                         "reason": "runtime_identity_observation_superseded"}, [], [])
            if observed_time == previous_time:
                if previous_report["identity"] != report["identity"]:
                    raise ValueError("runtime_identity_same_time_observation_conflict")
                incident = previous_report.get("incident")
                return ({**previous_report, "incident_record_id": incident["incident_id"] if incident else "",
                         "current_observation_record_id": current_id}, [], [])
        changed: list[RecordEnvelope] = []
        incident = report.get("incident")
        if isinstance(incident, Mapping):
            existing = sqlite.get_by_id(incident["incident_id"], scope=scope_ref, exact_scope=True)
            if existing is not None:
                physical = sqlite.list_records_by_sources_exact_scope(
                    scope=scope_ref, sources=[_SOURCE], kinds=["incident"],
                    record_ids=[incident["incident_id"]], limit=2)
                existing_report = existing.content.get("detector_report")
                if (len(physical) != 1 or physical[0].to_dict() != existing.to_dict()
                        or existing.scope != scope_ref or existing.kind != "incident" or existing.source != _SOURCE
                        or existing.source_id != "default"
                        or not validate_runtime_identity_drift_report(existing_report, scope=scope_ref)
                        or existing_report["identity"] != report["identity"]
                        or any(existing.content.get(key) != value for key, value in incident.items())
                        or existing.meta.get("incident_digest") != incident["incident_digest"]):
                    raise ValueError("runtime_identity_incident_identity_conflict")
                # Do not replace provenance, content, status or timestamps.
            else:
                record = RecordEnvelope.create(
                    kind="incident",
                    title=str(incident["title"]),
                    summary=str(incident["summary"]),
                    detail="Systemd effective environment differs from the current immutable release receipt.",
                    content={**dict(incident), "detector_report": report},
                    tags=["code-evolution", "runtime-identity", "system-detected"],
                    source=_SOURCE,
                    scope=scope_ref,
                    status="active",
                    provenance={
                        "origin": "system_detector",
                        "detector": DETECTOR_ID,
                        **report['source_facts']['facts'],
                    },
                    meta={
                        "idempotency_key": f"runtime-identity-drift:v2:{incident['incident_digest']}",
                        "incident_class": INCIDENT_CLASS,
                        "incident_digest": incident["incident_digest"],
                        "observation_valid": True,
                    },
                )
                record.record_id = str(incident["incident_id"])
                from eimemory.governance.evolution.managed_code_incident_proof import bind_native_activation
                bind_native_activation(record)
                sqlite.upsert(record, commit=False)
                changed.append(record)
        observation = RecordEnvelope.create(
            kind="reflection", title="Current native runtime identity observation",
            scope=scope_ref, source=CURRENT_SOURCE, status="active",
            content={"schema": CURRENT_SCHEMA, "report": report, "report_digest": _digest(report)},
            provenance={"origin": "system_detector", "detector": DETECTOR_ID},
            meta={"observation_valid": True},
        )
        observation.record_id = current_id
        if previous is not None:
            observation.time.created_at = previous.time.created_at
        sqlite.upsert(observation, commit=False)
        changed.append(observation)
        return ({**report, "incident_record_id": incident["incident_id"] if incident else "",
                 "current_observation_record_id": current_id}, changed, [])

    result = runtime.store.mutate_records_atomically(persist)
    if result.get('incident_record_id'):
        from eimemory.governance.evolution.managed_code_incident_proof import observe_managed_incident
        original = runtime.store.get_by_id(result['incident_record_id'], scope=scope_ref)
        result['managed_incident_proof'] = observe_managed_incident(runtime, scope=scope_ref, record=original)
    return result


def runtime_identity_current_record_id(scope: ScopeRef | Mapping[str, Any], expected_commit: str) -> str:
    """Locate one exact-owner/release current observation, never an alias."""
    commit = str(expected_commit or "").strip().lower()
    if _HEX40.fullmatch(commit) is None:
        raise ValueError("expected_commit must be a full Git SHA")
    return "runtime-identity-current-" + _digest({
        "schema": CURRENT_SCHEMA, "scope": asdict(_exact_scope(scope)),
        "expected_commit": commit,
    })


def current_runtime_identity_incident(
    store: Any, *, scope: ScopeRef | Mapping[str, Any], expected_commit: str,
) -> str | None:
    """Return current native incident digest, healthy ``''``, or unknown ``None``.

    This is a lifecycle guard, not a substitute for incident provenance/policy.
    A recovered or changed observation cannot make an old active incident eligible.
    """
    scope_ref = _exact_scope(scope)
    expected_commit = str(expected_commit or "").strip().lower()
    records = store.list_records_by_sources_exact_scope(
        scope=scope_ref, sources=[CURRENT_SOURCE], kinds=["reflection"],
        record_ids=[runtime_identity_current_record_id(scope_ref, expected_commit)], limit=2)
    if len(records) != 1:
        return None
    report = _current_report(records[0], scope_ref, expected_commit)
    if report is None:
        return None
    incident = report["incident"]
    return str(incident["incident_digest"]) if incident else ""


def _current_report(record: Any, scope: ScopeRef, expected_commit: str) -> dict[str, Any] | None:
    if (record is None or record.scope != scope or record.kind != "reflection"
            or record.source != CURRENT_SOURCE or record.source_id != "default" or record.status != "active"
            or record.provenance.get("origin") != "system_detector"
            or record.provenance.get("detector") != DETECTOR_ID
            or record.meta.get("observation_valid") is not True
            or record.content.get("schema") != CURRENT_SCHEMA):
        return None
    report = record.content.get("report")
    if (not validate_runtime_identity_drift_report(report, scope=scope)
            or report["expected_commit"] != expected_commit
            or record.content.get("report_digest") != _digest(report)):
        return None
    return dict(report)


def validate_runtime_identity_drift_report(report: Any, *, scope: ScopeRef | Mapping[str, Any]) -> bool:
    """Validate the explicit owner-bound v2 contract without inventing v1 facts."""
    try:
        scope_dict = asdict(_exact_scope(scope))
        if (not isinstance(report, Mapping) or report.get("schema") != REPORT_SCHEMA
                or report.get("detector") != DETECTOR_ID or report.get("scope") != scope_dict
                or not isinstance(report.get("expected_commit"), str)
                or _HEX40.fullmatch(report["expected_commit"]) is None):
            return False
        _observation_time(report.get("detected_at"))
        mismatches = report.get("mismatches")
        if not isinstance(mismatches, list):
            return False
        units: list[str] = []
        for item in mismatches:
            if (not isinstance(item, dict) or set(item) != {"unit", "reason", "observed_commit"}
                    or any(not isinstance(value, str) for value in item.values())
                    or not item["unit"] or item["observed_commit"] != item["observed_commit"].strip().lower()):
                return False
            reason, observed = item["reason"], item["observed_commit"]
            if reason in {"runtime_commit_missing", "runtime_commit_ambiguous"}:
                if observed:
                    return False
            elif reason != "commit_mismatch" or observed == report["expected_commit"]:
                return False
            units.append(item["unit"])
        if units != sorted(set(units)):
            return False
        identity = {"schema": IDENTITY_SCHEMA, "detector": DETECTOR_ID,
                    "expected_commit": report["expected_commit"], "scope": scope_dict,
                    "mismatches": mismatches}
        if 'source_facts' in report:
            from eimemory.contracts.code_incident_provenance import native_detection_provenance
            expected_facts = native_detection_provenance(DETECTOR_ID)
            if report['source_facts'] != expected_facts or any(
                    report.get(key) != value for key, value in expected_facts['facts'].items()):
                return False
            identity['source_facts_digest'] = expected_facts['digest']
        if report.get("identity") != identity or report.get("identity_digest") != _digest(identity):
            return False
        if (report.get("origin") != "system_detector"
                or any(report.get(field) is not None and type(report.get(field)) is not bool
                       for field in ('known_before_detection', 'prior_user_reported', 'manual_bootstrap'))
                or report.get("observation_valid") is not True
                or report.get("ok") is not (not mismatches)
                or report.get("status") != ("drift_detected" if mismatches else "current")):
            return False
        if not mismatches:
            return report.get("incident") is None
        # Regenerate only a new v2 contract from its claimed observation; legacy
        # records are never upgraded or rewritten by this validator.
        environments = {}
        for item in mismatches:
            environments[item["unit"]] = (
                "" if item["reason"] == "runtime_commit_missing" else (
                    "EIMEMORY_RUNTIME_COMMIT=x EIMEMORY_RUNTIME_COMMIT=y"
                    if item["reason"] == "runtime_commit_ambiguous" else
                    "EIMEMORY_RUNTIME_COMMIT=" + shlex.quote(item["observed_commit"])))
        expected = detect_runtime_identity_drift(expected_commit=report["expected_commit"],
                    unit_environments=environments, detected_at=report["detected_at"], scope=scope)
        return report.get("incident") == expected["incident"]
    except (TypeError, ValueError, AttributeError):
        return False


def _exact_scope(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    if isinstance(value, ScopeRef):
        return value
    if not isinstance(value, Mapping) or any(not isinstance(value.get(key), str)
            for key in ("tenant_id", "agent_id", "workspace_id", "user_id")):
        raise ValueError("runtime_identity_exact_owner_required")
    result = ScopeRef.from_dict(dict(value))
    if asdict(result) != dict(value):
        raise ValueError("runtime_identity_owner_must_not_be_normalized")
    return result


def _observation_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("runtime_identity_observation_time_required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("runtime_identity_observation_timezone_required")
    return result


def _systemd_environment(unit: str) -> str | None:
    from eimemory.core.runtime_manager import runtime_commit
    try:
        return "EIMEMORY_RUNTIME_COMMIT=" + runtime_commit(unit)
    except (OSError, ValueError, RuntimeError):
        return None


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DETECTOR_ID",
    "INCIDENT_CLASS",
    "PYTHON_RUNTIME_UNITS",
    "REPORT_SCHEMA",
    "CURRENT_SCHEMA",
    "CURRENT_SOURCE",
    "validate_runtime_identity_drift_report",
    "runtime_identity_current_record_id",
    "current_runtime_identity_incident",
    "detect_runtime_identity_drift",
    "inspect_live_runtime_identity",
]
