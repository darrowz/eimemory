"""Keep the actual nightly step observation behind a safe exact reference.

The private record contains the complete JSON report, including failed results
and unknown source facts. CLI/supervisor output contains only its digests and
operational state. Archiving a report never certifies repair or L5 qualification.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping

from eimemory.governance.learning.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import payload_digest

SCHEMA = "nightly_step_evidence.v1"
REFERENCE_SCHEMA = "nightly_step_evidence_reference.v1"
SOURCE = "eimemory.scheduler.step_evidence"
RELEASE_FIELDS = ("release_commit", "release_version", "deployment_receipt_id", "release_session_id")
_STATES = frozenset({"idle", "processed", "observed", "blocked", "skipped", "completed", "unavailable",
                     "failed", "incomplete", "complete"})


def _snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    # No default=str, bool coercion, truncation or source-fact normalization.
    value = json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if not isinstance(value, dict):
        raise ValueError("step_report_not_object")
    return value


def report_digest(report: Mapping[str, Any]) -> str:
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _reference(record: RecordEnvelope) -> dict[str, Any]:
    original = record.content["step_report"]
    return {
        "schema": REFERENCE_SCHEMA, "ok": True, "archive_verified": True,
        "archive_complete": True, "record_id": record.record_id, "source_id": record.source_id,
        "run_id": record.content["run_id"], "step": record.content["step"], "scope": asdict(record.scope),
        "envelope_digest": payload_digest(record.to_dict()),
        "report_digest": record.content["report_digest"],
        **{key: record.content.get(key, "") for key in RELEASE_FIELDS},
        "report_ok": original.get("ok") if type(original.get("ok")) is bool else None,
        "report_execution_ok": original.get("execution_ok")
            if type(original.get("execution_ok")) is bool else None,
        "report_status": original.get("status")
            if isinstance(original.get("status"), str) and original["status"] in _STATES else "not_reported",
        "repair_complete": original.get("repair_complete") if type(original.get("repair_complete")) is bool else None,
    }


def read_nightly_step_evidence(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any], run_id: str,
    reference: Mapping[str, Any], step: str, expected_release: Mapping[str, Any] | None = None,
) -> RecordEnvelope:
    """Read this exact archived observation; never borrow a latest/alias record."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    if not isinstance(run_id, str) or not run_id or not isinstance(reference, Mapping):
        raise ValueError("maintenance_reference_invalid")
    exact_lookup = getattr(runtime.store, "get_by_exact_ref", None)
    if callable(exact_lookup):
        record = exact_lookup(reference.get("record_id", ""), scope=scope_ref,
                              source_id=reference.get("source_id", ""))
    else:
        record = runtime.store.get_by_id(reference.get("record_id", ""), scope=scope_ref, exact_scope=True)
    if (record is None or record.scope != scope_ref or record.source != SOURCE
            or record.kind != "reflection" or record.status != "archived"
            or record.content.get("schema") != SCHEMA or record.content.get("report_type") != SCHEMA
            or record.content.get("run_id") != run_id or record.content.get("step") != step or record.content.get("scope") != asdict(scope_ref)
            or not isinstance(record.content.get("step_report"), dict)
            or record.content.get("report_digest") != report_digest(record.content["step_report"])
            or any(record.content.get(key, "") != record.meta.get(key, "") for key in RELEASE_FIELDS)
            or (expected_release is not None and any(record.content.get(key, "") != expected_release.get(key, "")
                                                      for key in RELEASE_FIELDS))
            or dict(reference) != _reference(record)):
        raise ValueError("maintenance_archive_binding_mismatch")
    return record


def archive_nightly_step_evidence(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any], run_id: str, step: str, report: Mapping[str, Any],
) -> dict[str, Any]:
    """Append once per actual run and verify the complete report after readback."""
    if not isinstance(run_id, str) or not run_id or step not in {"code_evolution", "native_l5_assessment_failure"}:
        raise ValueError("maintenance_run_id_missing")
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    original = _snapshot(report)
    from eimemory.governance.release.evidence_contract import current_release_identity, release_identity_payload
    release = current_release_identity(runtime, scope_ref)
    expected_release = release_identity_payload(release) if release is not None else {}
    record = append_learning_record_once(
        runtime, kind="reflection", title="Nightly native step observation",
        summary="Complete native maintenance report; see exact archived evidence.",
        scope=scope_ref, loop_id="supervisor", step_name="nightly_step_evidence",
        semantic_key=stable_semantic_key(SCHEMA, step, run_id, scope_ref), authority_tier="L0", status="archived",
        source=SOURCE,
        occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        content={"schema": SCHEMA, "report_type": SCHEMA, "run_id": run_id,
                 "scope": asdict(scope_ref), "step": step, "report_digest": report_digest(original), "step_report": original},
        meta={"report_type": SCHEMA, "step_evidence_run_id": run_id, "nightly_step": step},
    )
    reference = _reference(record)
    readback = read_nightly_step_evidence(runtime, scope=scope_ref, run_id=run_id,
                                             reference=reference, step=step, expected_release=expected_release)
    if readback.content["step_report"] != original:
        raise ValueError("maintenance_run_report_changed")
    return reference
