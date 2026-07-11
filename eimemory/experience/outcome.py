from __future__ import annotations

import json
from typing import Any

from eimemory.experience.capability_contract import (
    contract_source_ids,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.experience.diagnosis import diagnose_outcome
from eimemory.experience.sanitize import OutcomeSanitizationError, sanitize_outcome_payload
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


REPORT_TYPE = "outcome_trace"
SCHEMA_VERSION = "outcome_trace.v1"


def record_outcome_trace(runtime: Any, payload: dict[str, Any], scope: dict | ScopeRef | None = None) -> dict[str, Any]:
    error = _validate_outcome_trace(payload)
    if error:
        return {"ok": False, "error": error}
    scope_ref = _scope_ref(scope)
    contract: dict[str, Any] | None = None
    if "capability_contract" in payload:
        contract = normalize_capability_contract(payload.get("capability_contract"))
        error = validate_capability_contract(
            contract,
            expected_capability=str(payload.get("capability") or "").strip(),
            expected_case_id=str(payload.get("capability_case_id") or "").strip(),
        )
        if error:
            return {"ok": False, "error": error}
        for source_id in contract_source_ids(contract):
            if runtime.store.get_by_id(source_id, scope=scope_ref) is None:
                return {"ok": False, "error": f"capability contract source record unavailable in scope: {source_id}"}
        payload = {**payload, "capability_contract": contract}
    try:
        safe_payload = sanitize_outcome_payload(payload)
    except OutcomeSanitizationError as exc:
        return {"ok": False, "error": f"unsafe payload: {exc}"}

    existing = _existing_outcome_record(runtime, safe_payload, scope=scope_ref)
    if existing is not None:
        return {"ok": True, "record_id": existing.record_id, "kind": existing.kind, "idempotent": True}

    diagnosis = diagnose_outcome(safe_payload)
    trace_id = _trace_id(safe_payload)
    idempotency_key = _idempotency_key(safe_payload)
    primary_label = str(diagnosis.get("primary_label") or "unknown_failure")
    signals = list(diagnosis.get("signals") or [])
    content = {
        "schema_version": SCHEMA_VERSION,
        "payload": safe_payload,
        "diagnosis": diagnosis,
    }
    for key in ("world_state", "visual_evidence", "operator_gap", "policy_attribution"):
        if key in safe_payload:
            content[key] = safe_payload[key]
    risk_level = _risk_level(safe_payload, diagnosis)
    task_type = str(safe_payload.get("task_type") or "")
    outcome_status = _outcome_status(safe_payload.get("outcome"))
    business_meta = {
        "report_type": REPORT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "idempotency_key": idempotency_key,
        "task_type": task_type,
        "primary_label": primary_label,
        "diagnosis_signals": signals,
        "signals": signals,
        "risk_level": risk_level,
        "outcome_status": outcome_status,
    }
    if contract is not None:
        business_meta.update(
            {
                "capability": str(contract.get("capability") or ""),
                "capability_case_id": str(contract.get("case_id") or ""),
                "contract_verified": True,
            }
        )
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"Outcome trace: {trace_id}",
        summary=f"{primary_label}: {safe_payload.get('input_summary') or trace_id}",
        detail=_brief_detail(content),
        content=content,
        tags=["experience", REPORT_TYPE, primary_label],
        source="eimemory.experience.outcome_trace",
        scope=scope_ref,
        provenance={
            "report_type": REPORT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
        },
        meta={
            **business_meta,
            "business_meta": business_meta,
            "report_type": REPORT_TYPE,
            "schema_version": SCHEMA_VERSION,
        },
    )
    stored = runtime.store.append(record)
    return {"ok": True, "record_id": stored.record_id, "kind": stored.kind, "idempotent": False}


def _validate_outcome_trace(payload: object) -> str:
    if not isinstance(payload, dict):
        return "payload must be an object"
    if not _trace_id(payload):
        return "trace_id is required"
    if "outcome" not in payload:
        return "outcome is required"
    if "actions" in payload and not isinstance(payload.get("actions"), list):
        return "actions must be a list"
    if "selected_tools" in payload and not isinstance(payload.get("selected_tools"), list):
        return "selected_tools must be a list"
    return ""


def _existing_outcome_record(runtime: Any, payload: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope | None:
    trace_id = _trace_id(payload)
    idempotency_key = _idempotency_key(payload)
    if not trace_id and not idempotency_key:
        return None
    page_size = 500
    offset = 0
    while True:
        records = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=page_size, offset=offset)
        for record in records:
            if not _same_scope(record.scope, scope):
                continue
            if str(record.source or "") != "eimemory.experience.outcome_trace":
                continue
            meta = business_metadata(record.meta)
            if str(meta.get("report_type") or record.provenance.get("report_type") or "") != REPORT_TYPE:
                continue
            if idempotency_key and str(meta.get("idempotency_key") or record.provenance.get("idempotency_key") or "") == idempotency_key:
                return record
            if trace_id and str(meta.get("trace_id") or record.provenance.get("trace_id") or "") == trace_id:
                return record
        if len(records) < page_size:
            break
        offset += page_size
    return None


def _scope_ref(scope: dict | ScopeRef | None) -> ScopeRef:
    if isinstance(scope, ScopeRef):
        return scope
    return ScopeRef.from_dict(scope)


def _same_scope(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.agent_id == right.agent_id
        and left.workspace_id == right.workspace_id
        and left.user_id == right.user_id
    )


def _brief_detail(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1200]


def _trace_id(payload: dict[str, Any]) -> str:
    return str(payload.get("trace_id") or _nested(payload, "trace_context", "trace_id") or "").strip()


def _idempotency_key(payload: dict[str, Any]) -> str:
    return str(payload.get("idempotency_key") or _nested(payload, "trace_context", "idempotency_key") or "").strip()


def _risk_level(payload: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    if str(diagnosis.get("primary_label") or "") == "unsafe_or_high_risk":
        return "high"
    for value in (
        payload.get("risk_level"),
        payload.get("safety_level"),
        _nested(payload, "risk", "level"),
        _nested(payload, "risk", "severity"),
        _nested(payload, "safety", "risk_level"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "low" if diagnosis.get("primary_label") == "success" else "medium"


def _outcome_status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("outcome") or value.get("result") or "").strip()
    return str(value or "").strip()


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
