"""System detector for stale release identities in Python runtime units."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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


def detect_runtime_identity_drift(
    *,
    expected_commit: str,
    unit_environments: Mapping[str, str],
    detected_at: str,
) -> dict[str, Any]:
    """Compare effective systemd environments with one release identity."""

    commit = str(expected_commit or "").strip().lower()
    if _HEX40.fullmatch(commit) is None:
        raise ValueError("expected_commit must be a full Git SHA")
    if not isinstance(unit_environments, Mapping) or not unit_environments:
        raise ValueError("unit_environments must be a non-empty mapping")
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
    incident: dict[str, Any] | None = None
    if mismatches:
        digest = _digest(material)
        units = ", ".join(item["unit"] for item in mismatches)
        incident = {
            "incident_id": f"incident-runtime-identity-{digest[:24]}",
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
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "observation_valid": True,
        "incident": incident,
    }


def inspect_live_runtime_identity(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    detected_at: str,
    runner: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Read live systemd authority and persist one idempotent system incident."""

    from eimemory.governance.evidence_contract import current_release_identity

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    release = current_release_identity(runtime, scope_ref)
    if release is None:
        return {"ok": False, "status": "unavailable", "reason": "current_release_identity_unavailable"}
    read_environment = runner or _systemd_environment
    environments = {unit: read_environment(unit) for unit in PYTHON_RUNTIME_UNITS}
    report = detect_runtime_identity_drift(
        expected_commit=release.commit,
        unit_environments=environments,
        detected_at=detected_at,
    )
    incident = report.get("incident")
    if not isinstance(incident, Mapping):
        return {**report, "incident_record_id": ""}
    marker = f"runtime-identity-drift:{incident['incident_digest']}"
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
                detail="Systemd effective environment differs from the current immutable release receipt.",
                content={**dict(incident), "detector_report": report},
                tags=["code-evolution", "runtime-identity", "system-detected"],
                source="eimemory.runtime_identity_drift",
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


def _systemd_environment(unit: str) -> str:
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=Environment", "--value"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DETECTOR_ID",
    "INCIDENT_CLASS",
    "PYTHON_RUNTIME_UNITS",
    "detect_runtime_identity_drift",
    "inspect_live_runtime_identity",
]
