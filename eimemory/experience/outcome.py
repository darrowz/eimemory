from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from eimemory.experience.capability_contract import (
    contract_source_ids,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.experience.diagnosis import diagnose_outcome
from eimemory.experience.sanitize import OutcomeSanitizationError, sanitize_outcome_payload
from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.adapters.runtime.channel import base_scope_from_channel


REPORT_TYPE = "outcome_trace"
SCHEMA_VERSION = "outcome_trace.v1"


class OutcomeTraceBuildError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OutcomeTraceRecordBuild:
    record: RecordEnvelope
    payload: dict[str, Any]
    contract: dict[str, Any] | None


def record_outcome_trace(runtime: Any, payload: dict[str, Any], scope: dict | ScopeRef | None = None) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    payload = dict(payload)
    payload.setdefault("recorded_at", now_iso())
    if _server_bound_real_task(payload):
        for key in (
            "release_commit",
            "release_version",
            "deployment_receipt_id",
            "release_session_id",
            "evidence_class",
        ):
            payload.pop(key, None)
        release_scope = _release_scope_for_real_task(payload, scope_ref)
        release = current_release_identity(runtime, release_scope)
        if release is not None:
            payload.update(release_identity_payload(release))
            payload["evidence_class"] = "verified_real_task"
    try:
        build = build_outcome_trace_record(payload, scope=scope_ref)
    except OutcomeTraceBuildError as exc:
        return {"ok": False, "error": str(exc)}
    for source_id in contract_source_ids(build.contract or {}):
        if runtime.store.get_by_id(source_id, scope=scope_ref) is None:
            return {"ok": False, "error": f"capability contract source record unavailable in scope: {source_id}"}
    existing = _existing_outcome_record(runtime, build.payload, scope=scope_ref)
    if existing is not None:
        return {"ok": True, "record_id": existing.record_id, "kind": existing.kind, "idempotent": True}
    stored = runtime.store.append(build.record)
    return {"ok": True, "record_id": stored.record_id, "kind": stored.kind, "idempotent": False}


def build_outcome_trace_record(
    payload: dict[str, Any],
    *,
    scope: ScopeRef | dict | None = None,
) -> OutcomeTraceRecordBuild:
    """Validate and build one outcome-trace record without reading or writing runtime state."""
    error = _validate_outcome_trace(payload)
    if error:
        raise OutcomeTraceBuildError(error)
    scope_ref = _scope_ref(scope)
    normalized_payload = dict(payload)
    normalized_payload["recorded_at"] = _canonical_recorded_at(
        normalized_payload.get("recorded_at")
    )
    contract: dict[str, Any] | None = None
    if "capability_contract" in normalized_payload:
        contract = normalize_capability_contract(normalized_payload.get("capability_contract"))
        error = validate_capability_contract(
            contract,
            expected_capability=str(normalized_payload.get("capability") or "").strip(),
            expected_case_id=str(normalized_payload.get("capability_case_id") or "").strip(),
        )
        if error:
            raise OutcomeTraceBuildError(error)
        outcome = normalized_payload.get("outcome")
        if contract.get("probe") is True and (
            not isinstance(outcome, dict) or outcome.get("rehearsal") is not True
        ):
            raise OutcomeTraceBuildError(
                "capability contract probe requires outcome.rehearsal to be true"
            )
        normalized_payload["capability_contract"] = contract
    try:
        safe_payload = sanitize_outcome_payload(normalized_payload)
    except OutcomeSanitizationError as exc:
        raise OutcomeTraceBuildError(f"unsafe payload: {exc}") from exc
    diagnosis = diagnose_outcome(safe_payload)
    trace_id = _trace_id(safe_payload)
    idempotency_key = _idempotency_key(safe_payload)
    primary_label = str(diagnosis.get("primary_label") or "unknown_failure")
    blame_layer = str(diagnosis.get("blame_layer") or "unknown")
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
        "blame_layer": blame_layer,
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
    record.record_id = _outcome_trace_record_id(
        scope=scope_ref,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
    )
    recorded_at = str(safe_payload["recorded_at"])
    record.time = TimeRef(
        created_at=recorded_at,
        updated_at=recorded_at,
        occurred_at=recorded_at,
    )
    return OutcomeTraceRecordBuild(record=record, payload=safe_payload, contract=contract)


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


def _server_bound_real_task(payload: dict[str, Any]) -> bool:
    source = str(payload.get("source") or "").strip()
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    return source in {
        "openclaw.agent_end",
        "openclaw.task_end",
        "codex.stop",
        "hermes.task_end",
    } and outcome.get("rehearsal") is False


def _release_scope_for_real_task(payload: dict[str, Any], scope: ScopeRef) -> ScopeRef:
    source = str(payload.get("source") or "").strip()
    channel = source.split(".", 1)[0] if "." in source else ""
    if channel in {"codex", "hermes"}:
        return ScopeRef.from_dict(base_scope_from_channel(channel, scope))
    return scope


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


def _outcome_trace_record_id(*, scope: ScopeRef, trace_id: str, idempotency_key: str) -> str:
    stable = json.dumps(
        {
            "scope": {
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            },
            "operation": "outcome_trace",
            "idempotency_key": idempotency_key or trace_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ref_" + sha256(stable.encode("utf-8")).hexdigest()[:32]


def _canonical_recorded_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise OutcomeTraceBuildError("recorded_at is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeTraceBuildError("recorded_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise OutcomeTraceBuildError("recorded_at must include a timezone")
    return parsed.isoformat()


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
