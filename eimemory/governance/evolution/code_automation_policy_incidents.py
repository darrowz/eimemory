"""Protected native current-incident authority shared by issuance and routing."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from eimemory.governance.evolution.code_evolution_test_plans import (
    INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
    RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    RELEASE_REPORT_FAILURE_TEST_PLAN_ID,
    RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
)
from eimemory.ops.system_code_repair_failure import (
    DETECTOR_ID as SYSTEM_REPAIR_FAILURE_DETECTOR_ID,
    INCIDENT_CLASS as SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS,
    SOURCE as SYSTEM_REPAIR_FAILURE_SOURCE,
)
from eimemory.models.records import ScopeRef


_ROUTES = {
    "deployment.runtime_commit_drift": (
        "eimemory.runtime_identity_drift",
        RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    ),
    "release.closure_internal_failure": (
        "eimemory.release_closure_failure",
        RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    ),
    "release.closure_report_failure": (
        "eimemory.release_closure_failure",
        RELEASE_REPORT_FAILURE_TEST_PLAN_ID,
    ),
    SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS: (
        SYSTEM_REPAIR_FAILURE_SOURCE,
        INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
    ),
}
_INCIDENT_FIELDS = (
    "incident_id",
    "incident_digest",
    "incident_class",
    "title",
    "summary",
    "diagnostic_codes",
    "acceptance_requirements",
)


def _resolve_repo_root(repo_root: str | Path | None) -> str:
    if repo_root is not None and str(repo_root).strip():
        return str(repo_root)
    from eimemory.config.trusted import trusted_repository_root
    return str(trusted_repository_root())


def _trusted_incident(record: Any, base_commit: str, *, diagnosis: bool = False) -> dict[str, Any] | None:
    if getattr(record, "status", None) != "active":
        return None
    return _trusted_incident_identity(record, base_commit, diagnosis=diagnosis)


def _trusted_incident_identity(record: Any, base_commit: str, *, diagnosis: bool = False) -> dict[str, Any] | None:
    """Validate detector identity before classifying its lifecycle state."""
    provenance = getattr(record, "provenance", {})
    meta = getattr(record, "meta", {})
    content = getattr(record, "content", {})
    if not isinstance(provenance, Mapping) or not isinstance(meta, Mapping) or not isinstance(content, Mapping):
        return None
    if (
        provenance.get("origin") != "system_detector"
        or meta.get("observation_valid") is not True
    ):
        return None
    detector_report = content.get("detector_report")
    if (
        not isinstance(detector_report, Mapping)
        or detector_report.get("origin") != "system_detector"
        or detector_report.get("observation_valid") is not True
    ):
        return None
    try:
        incident_source_facts(record)
    except (TypeError, ValueError):
        return None
    incident = {field: content.get(field) for field in _INCIDENT_FIELDS}
    if (
        any(value is None for value in incident.values())
        or str(incident["incident_class"] or "") not in _ROUTES
        or str(meta.get("incident_digest") or "") != str(incident["incident_digest"] or "")
    ):
        return None
    source, _ = _ROUTES[incident["incident_class"]]
    if (getattr(record, "source", None) != source
            or provenance.get("detector") != source + ".v1"):
        return None
    if diagnosis and (detector_report.get("schema") != "release_closure_failure.v2"
                      or detector_report.get("status") != "diagnosis_required"):
        return None
    if incident["incident_class"] in {"release.closure_internal_failure", "release.closure_report_failure"}:
        if detector_report.get("release_commit") != base_commit:
            return None
        if detector_report.get("schema") == "release_closure_failure.v2":
            from eimemory.governance.release.closure_verdict import report_digest
            from dataclasses import asdict

            identity = detector_report.get("identity")
            if (detector_report.get("status") != ("diagnosis_required" if diagnosis else "failure_detected")
                    or detector_report.get("repair_eligible") is not (False if diagnosis else True)
                    or detector_report.get("detector") != "eimemory.release_closure_failure.v1"
                    or provenance.get("detector") != "eimemory.release_closure_failure.v1"
                    or not isinstance(identity, Mapping)
                    or identity.get("schema") != "release_closure_incident_identity.v2"
                    or identity.get("detector") != "eimemory.release_closure_failure.v1"
                    or identity.get("status") != detector_report.get("status")
                    or identity.get("scope") != asdict(record.scope)
                    or identity.get("incident_class") != incident["incident_class"]
                    or identity.get("release_commit") != base_commit
                    or identity.get("report_digest") != detector_report.get("report_digest")
                    or detector_report.get("incident") != incident
                    or report_digest(dict(identity)) != incident["incident_digest"]):
                return None
        elif incident["incident_class"] == "release.closure_report_failure":
            return None
    elif incident["incident_class"] == "deployment.runtime_commit_drift":
        from eimemory.ops.runtime_identity_drift import REPORT_SCHEMA, validate_runtime_identity_drift_report
        if (detector_report.get("expected_commit") != base_commit
                or detector_report.get("schema") != REPORT_SCHEMA
                or not validate_runtime_identity_drift_report(detector_report, scope=record.scope)
                or detector_report.get("incident") != incident
                or detector_report.get("identity_digest") != incident["incident_digest"]
                or getattr(record, "record_id", None) != incident["incident_id"]):
            return None
    elif incident["incident_class"] == SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS:
        from eimemory.ops.system_code_repair_failure import validate_system_code_repair_failure_report
        if (
            detector_report.get("detector") != SYSTEM_REPAIR_FAILURE_DETECTOR_ID
            or detector_report.get("release_commit") != base_commit
            or not validate_system_code_repair_failure_report(detector_report)
            or detector_report.get("incident") != incident
        ):
            return None
    return incident


def incident_source_facts(record: Any) -> dict:
    """Repair admission checks original source; novelty is a separate verdict."""
    from eimemory.contracts.code_incident_provenance import (
        FIELDS, verified_provenance, qualification_provenance, native_detection_provenance,
    )
    report = record.content['detector_report']
    detector = record.provenance.get('detector')
    if 'source_facts' in report:
        facts = verified_provenance(report['source_facts'])
        # Producers can preserve positive adverse facts, never invent negatives.
        expected = native_detection_provenance(detector, facts)
        if report['source_facts'] != expected:
            raise ValueError('native_negative_facts_unproven')
        if any(report.get(k) != v or record.provenance.get(k) != v for k, v in facts.items()):
            raise ValueError('native_source_facts_mismatch')
        identity = report.get('identity')
        if isinstance(identity, Mapping) and identity.get('source_facts_digest') != report['source_facts']['digest']:
            raise ValueError('native_source_facts_unbound')
    else:
        facts = {'origin': report.get('origin'), 'detector': detector,
                 **{key: report.get(key) for key in FIELDS}}
        if any(value is not None and type(value) is not bool for key, value in facts.items() if key in FIELDS):
            raise ValueError('native_source_facts_invalid')
        facts = qualification_provenance(facts)
    return {k: facts.get(k) for k in ('origin', 'detector', *FIELDS)}


def _repository_identity(root: Path) -> dict[str, Any]:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip().lower()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"ok": False, "reason": "repository_identity_unavailable"}
    if status:
        return {"ok": False, "reason": "repository_worktree_not_clean"}
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        return {"ok": False, "reason": "repository_commit_invalid"}
    return {"ok": True, "base_commit": commit}




def select_current_system_incidents(runtime: Any, *, scope: ScopeRef, root: Path, repository: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from eimemory.governance.release.evidence_contract import current_release_identity
    scope_ref = scope
    repository = dict(repository) if repository is not None else _repository_identity(root)
    if repository.get("ok") is not True:
        return {"ok": False, "status": "blocked", "reason": repository.get("reason"), "processed": []}
    release = current_release_identity(runtime, scope_ref)
    if release is None or release.commit != repository["base_commit"]:
        return {"ok": False, "status": "blocked", "reason": "repository_release_identity_mismatch", "processed": []}
    try:
        records = runtime.store.list_records_by_sources_exact_scope(
            kinds=["incident"], sources=list(dict.fromkeys(route[0] for route in _ROUTES.values())),
            scope=scope_ref, limit=501)
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error):
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_unavailable",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    if not isinstance(records, list) or len(records) > 500:
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_incomplete",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    sources = {route[0] for route in _ROUTES.values()}
    if any(getattr(record, "scope", None) != scope_ref or getattr(record, "source", None) not in sources
           or getattr(record, "kind", None) != "incident" for record in records):
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_mismatched",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    unknown_lifecycle = []
    for record in records:
        if (getattr(record, "status", None) not in {"active", "resolved", "archived", "quarantined", "inactive", "superseded", "rejected"}
                and (_trusted_incident_identity(record, repository["base_commit"]) is not None
                     or _trusted_incident_identity(record, repository["base_commit"], diagnosis=True) is not None)):
            unknown_lifecycle.append(record.record_id)
    if unknown_lifecycle:
        return {"ok": False, "status": "blocked", "reason": "current_incident_lifecycle_unverified",
                "repair_complete": False, "pending_incident_record_ids": unknown_lifecycle, "processed": []}
    current = [(record, incident) for record in records
               if (incident := _trusted_incident(record, repository["base_commit"])) is not None]
    invalid_current = []
    for record in records:
        report = getattr(record, 'content', {}).get('detector_report', {})
        if (getattr(record, 'status', None) == 'active' and isinstance(report, Mapping)
                and (report.get('release_commit') == repository['base_commit']
                     or report.get('expected_commit') == repository['base_commit'])
                and _trusted_incident(record, repository['base_commit']) is None
                and _trusted_incident(record, repository['base_commit'], diagnosis=True) is None):
            invalid_current.append(record.record_id)
    if invalid_current:
        return {'ok': False, 'status': 'blocked', 'reason': 'current_incident_source_unverified',
                'pending_incident_record_ids': invalid_current, 'processed': []}
    if any(incident["incident_class"] == "deployment.runtime_commit_drift" for _, incident in current):
        from eimemory.ops.runtime_identity_drift import current_runtime_identity_incident
        try:
            current_digest = current_runtime_identity_incident(
                runtime.store, scope=scope_ref, expected_commit=repository["base_commit"])
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error):
            current_digest = None
        if current_digest is None:
            return {"ok": False, "status": "blocked", "reason": "runtime_identity_current_observation_unverified",
                    "repair_complete": False, "processed": []}
        current = [(record, incident) for record, incident in current
                   if incident["incident_class"] != "deployment.runtime_commit_drift"
                   or incident["incident_digest"] == current_digest]
    if any(i['incident_class'] == SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS for _, i in current):
        from eimemory.ops.system_code_repair_failure import current_system_code_repair_failure
        latest = current_system_code_repair_failure(runtime.store, scope=scope_ref,
                                                     expected_commit=repository['base_commit'])
        if latest is None:
            return {'ok': False, 'status': 'blocked', 'reason': 'repair_current_observation_unverified',
                    'repair_complete': False, 'processed': []}
        current = [(r, i) for r, i in current if i['incident_class'] != SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS
                   or i['incident_digest'] == latest]
    diagnosis = [record.record_id for record in records
                 if _trusted_incident(record, repository["base_commit"], diagnosis=True) is not None]
    if any(i['incident_class'].startswith('release.closure_') for _, i in current) or diagnosis:
        from eimemory.governance.evolution.code_automation_policy_capture import current_closure_incident_id
        try:
            latest = current_closure_incident_id(runtime, scope=scope_ref, release=release)
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
            return {"ok": False, "status": "blocked", "reason": "closure_current_capture_unverified",
                    "repair_complete": False, "processed": []}
        if latest is not None:
            current = [(r, i) for r, i in current if not i['incident_class'].startswith('release.closure_')
                       or r.record_id == latest]
            diagnosis = [r for r in diagnosis if r == latest]
    if not current:
        if diagnosis:
            return {"ok": False, "status": "diagnosis_required", "processed": [],
                    "reason": "closure_diagnosis_not_completed", "repair_complete": False,
                    "pending_incident_record_ids": diagnosis}
        return {"ok": True, "status": "idle", "processed": []}
    return {"ok": True, "status": "current", "current": current, "repository": repository, "release": release}
