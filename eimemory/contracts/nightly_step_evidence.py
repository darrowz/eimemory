"""Safe public projection of private native step evidence and failure stages."""
from __future__ import annotations

import re
from typing import Any

REFERENCE_SCHEMA = "nightly_step_evidence_reference.v1"
NATIVE_L5_STAGES = frozenset({
    "scope_selection", "provider_authority", "release_authority", "readiness_reader",
    "envelope_validation", "evidence_classification", "normalized_assessment",
    "release_recheck", "completion_validation", "archive_write", "archive_readback",
    "provider_authority_recheck", "result_projection", "controlled_release_lineage",
})
NATIVE_L5_ERROR_CODES = frozenset({
    "native_l5_current_release_unverified", "native_l5_envelope_contract_invalid",
    "native_l5_normalized_assessment_not_persisted", "native_l5_release_authority_changed",
    "native_l5_completion_contract_invalid", "native_l5_archive_missing_after_write",
    "native_l5_archive_authority_mismatch", "native_l5_reader_execution_failed",
    "native_l5_execution_failed",
    "native_l5_controlled_lineage_revalidation_failed",
})
_REFERENCE_FIELDS = frozenset({
    "schema", "ok", "archive_verified", "archive_complete", "record_id", "source_id",
    "run_id", "step", "scope", "envelope_digest", "report_digest", "release_commit",
    "release_version", "deployment_receipt_id", "release_session_id", "report_ok",
    "report_execution_ok", "report_status", "repair_complete",
})
_STATES = frozenset({"idle", "processed", "observed", "blocked", "skipped", "completed",
                     "unavailable", "failed", "incomplete", "complete", "not_reported"})


def exception_type_name(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"


def step_evidence_diagnostics(value: Any) -> dict[str, Any]:
    """Expose only the complete fixed reference; never arbitrary error/body text."""
    unavailable = {"schema": REFERENCE_SCHEMA, "ok": False, "archive_verified": False,
                   "archive_complete": False, "reason": "nightly_step_evidence_unverified"}
    if not isinstance(value, dict) or set(value) != _REFERENCE_FIELDS:
        return unavailable
    if (value.get("schema") != REFERENCE_SCHEMA or value.get("ok") is not True
            or value.get("archive_verified") is not True or value.get("archive_complete") is not True
            or not isinstance(value.get("step"), str)
            or value["step"] not in {"code_evolution", "native_l5_assessment_failure"}
            or not isinstance(value.get("report_status"), str) or value["report_status"] not in _STATES
            or any(type(value.get(key)) is not bool and value.get(key) is not None
                   for key in ("report_ok", "report_execution_ok", "repair_complete"))
            or any(not isinstance(value.get(key), str) or not 0 < len(value[key]) <= 256
                   for key in ("record_id", "source_id", "run_id"))
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                   for key in ("envelope_digest", "report_digest"))
            or not isinstance(value.get("scope"), dict)
            or set(value["scope"]) != {"tenant_id", "agent_id", "workspace_id", "user_id"}
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in value["scope"].values())
            or any(not isinstance(value.get(key), str) or len(value[key]) > 256
                   for key in ("release_commit", "release_version", "deployment_receipt_id", "release_session_id"))):
        return unavailable
    return {**value, "scope": dict(value["scope"])}


def native_l5_failure_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value.get("failure_stage"), str) and value["failure_stage"] in NATIVE_L5_STAGES:
        result["failure_stage"] = value["failure_stage"]
    if isinstance(value.get("error"), str) and value["error"] in NATIVE_L5_ERROR_CODES:
        result["error"] = value["error"]
    error_type = value.get("error_type")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
        result["error_type"] = error_type
    if "failure_evidence" in value:
        result["failure_evidence"] = step_evidence_diagnostics(value["failure_evidence"])
    return result
