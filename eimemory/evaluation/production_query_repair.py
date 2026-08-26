"""Bounded repair for production-query evidence flattened to a base scope."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any, Callable

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.core.clock import now_iso
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_QUERY_SCHEMA,
    ACCEPTED_SOURCE,
    LABEL_EVIDENCE_SOURCE,
    PENDING_QUERY_SCHEMA,
    PENDING_SOURCE,
    accepted_production_query_validation_error,
    pending_production_query_capture_validation_error,
)
from eimemory.evaluation.real_query_gate import (
    PRODUCTION_REAL_QUERY_TRUSTED_LABELERS,
    _stable_digest,
)
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef


REPAIR_SCHEMA = "production_query_channel_scope_repair.v1"
REPAIR_SOURCE = "eimemory.production_recall.scope_repair"
_MAX_CONFLICTS = 50
_REPORT_TYPES = (
    ("pending", "production_recall_pending_case", PENDING_SOURCE),
    ("label", "production_recall_label_evidence", LABEL_EVIDENCE_SOURCE),
    ("accepted", "production_recall_accepted_case", ACCEPTED_SOURCE),
)


def repair_production_query_channel_scopes(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500,
    persist_receipt: bool = True,
) -> dict[str, Any]:
    """Restore exact channel scopes from mutually consistent structured evidence.

    The scan is intentionally limited to known report types at the supplied
    base scope. Query text and memory bodies are neither read into the report
    nor persisted in its receipt.
    """

    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    bounded = max(1, min(500, int(limit)))
    result: dict[str, Any] = {
        "schema": REPAIR_SCHEMA,
        "ok": True,
        "scanned_count": 0,
        "repaired_count": 0,
        "already_correct_count": 0,
        "conflict_count": 0,
        "overflow_count": 0,
        "by_type": {},
        "by_channel": {},
        "repaired_record_ids": [],
        "conflicts": [],
    }
    validators: dict[str, Callable[[Any, RecordEnvelope, ScopeRef, ScopeRef], tuple[ScopeRef | None, str]]] = {
        "pending": _validate_pending,
        "label": _validate_label,
        "accepted": _validate_accepted,
    }
    batches: dict[str, list[RecordEnvelope]] = {}
    for record_type, report_type, expected_source in _REPORT_TYPES:
        type_counts = result["by_type"].setdefault(
            record_type,
            {"scanned": 0, "repaired": 0, "already_correct": 0, "conflicts": 0},
        )
        total = runtime.store.count_records_by_meta_value(
            kinds=["evaluation_packet"],
            scope=base,
            meta_key="report_type",
            meta_value=report_type,
            status="active",
        )
        if total is None:
            _add_conflict(result, record_type, "", "indexed_record_count_unavailable")
            continue
        if int(total) > bounded:
            result["overflow_count"] += int(total) - bounded
            type_counts["available"] = int(total)
            type_counts["limit"] = bounded
            _add_conflict(result, record_type, "", "indexed_record_scan_overflow")
            continue
        records = runtime.store.list_records_by_meta_value(
            kinds=["evaluation_packet"],
            scope=base,
            meta_key="report_type",
            meta_value=report_type,
            status="active",
            limit=bounded,
        )
        if records is None:
            _add_conflict(result, record_type, "", "indexed_record_scan_unavailable")
            continue
        if len(records) != int(total):
            _add_conflict(result, record_type, "", "indexed_record_count_scan_mismatch")
            continue
        batches[record_type] = records

    if result["conflict_count"]:
        result["ok"] = False
        if persist_receipt:
            result["receipt_id"] = _persist_receipt(runtime, base=base, result=result)
        return result

    for record_type, _report_type, expected_source in _REPORT_TYPES:
        records = batches.get(record_type, [])
        type_counts = result["by_type"][record_type]
        for record in records:
            result["scanned_count"] += 1
            type_counts["scanned"] += 1
            if record.source != expected_source:
                _add_conflict(result, record_type, record.record_id, "source_mismatch")
                continue
            target, reason = validators[record_type](runtime, record, base, base)
            if target is None:
                _add_conflict(result, record_type, record.record_id, reason or "evidence_boundary_invalid")
                continue
            channel = _channel_for_record(record_type, record)
            channel_counts = result["by_channel"].setdefault(
                channel,
                {"scanned": 0, "repaired": 0, "already_correct": 0, "conflicts": 0},
            )
            channel_counts["scanned"] += 1
            if same_scope(record.scope, target):
                result["already_correct_count"] += 1
                type_counts["already_correct"] += 1
                channel_counts["already_correct"] += 1
                continue
            existing = runtime.store.get_by_id(record.record_id, scope=target)
            if existing is not None:
                _add_conflict(result, record_type, record.record_id, "target_scope_collision", channel=channel)
                continue
            moved = RecordEnvelope.from_dict(record.to_dict())
            moved.scope = target
            try:
                runtime.store.rewrite(moved, previous_scope=record.scope)
            except (OSError, RuntimeError, TypeError, ValueError):
                _add_conflict(result, record_type, record.record_id, "scope_rewrite_failed", channel=channel)
                continue
            result["repaired_count"] += 1
            type_counts["repaired"] += 1
            channel_counts["repaired"] += 1
            if len(result["repaired_record_ids"]) < _MAX_CONFLICTS:
                result["repaired_record_ids"].append(record.record_id)

    result["ok"] = result["conflict_count"] == 0
    result["repaired_record_ids"].sort()
    if persist_receipt:
        result["receipt_id"] = _persist_receipt(runtime, base=base, result=result)
    return result


def _validate_pending(
    runtime: Any,
    record: RecordEnvelope,
    base: ScopeRef,
    _unused: ScopeRef,
) -> tuple[ScopeRef | None, str]:
    payload = record.content if isinstance(record.content, dict) else {}
    if payload.get("schema") != PENDING_QUERY_SCHEMA:
        return None, "pending_schema_mismatch"
    target, reason = _target_scope(payload.get("channel"), payload.get("scope"), base)
    if target is None:
        return None, reason
    source_id = str(payload.get("source_id") or "")
    refs = payload.get("candidate_refs")
    if not source_id or source_id != record.source_id or not isinstance(refs, list) or not 1 <= len(refs) <= 5:
        return None, "pending_source_or_refs_invalid"
    for ref in refs:
        candidate = runtime.store.get_by_id(str(ref or ""), scope=target)
        if candidate is None or candidate.status != "active" or candidate.source_id != source_id:
            return None, "pending_candidate_boundary_invalid"
    moved = RecordEnvelope.from_dict(record.to_dict())
    moved.scope = target
    capture_error = pending_production_query_capture_validation_error(
        runtime,
        moved,
        exact_scope=target,
        channel=str(payload.get("channel") or ""),
    )
    if capture_error:
        return None, capture_error
    return target, ""


def _validate_label(
    runtime: Any,
    record: RecordEnvelope,
    base: ScopeRef,
    _unused: ScopeRef,
) -> tuple[ScopeRef | None, str]:
    payload = record.content if isinstance(record.content, dict) else {}
    pending_id = str(payload.get("pending_record_id") or "")
    record_ref = str(payload.get("record_ref") or "")
    labeler = str(payload.get("labeler") or "")
    grade = payload.get("grade")
    packet = payload.get("operator_packet_evidence") if isinstance(payload.get("operator_packet_evidence"), dict) else {}
    packet_digest = str(packet.get("digest") or "").lower()
    if (
        set(payload) != {
            "evidence_class",
            "labeler",
            "pending_record_id",
            "record_ref",
            "grade",
            "operator_packet_evidence",
        }
        or payload.get("evidence_class") != "operator_relevance_label"
        or not pending_id
        or not record_ref
        or labeler not in PRODUCTION_REAL_QUERY_TRUSTED_LABELERS
        or isinstance(grade, bool)
        or not isinstance(grade, int)
        or not 1 <= grade <= 3
        or packet.get("schema") != "secure_dataset_fingerprint.v1"
        or re.fullmatch(r"[0-9a-f]{64}", packet_digest) is None
        or isinstance(packet.get("size"), bool)
        or not isinstance(packet.get("size"), int)
        or int(packet.get("size") or 0) <= 0
        or isinstance(packet.get("device"), bool)
        or not isinstance(packet.get("device"), int)
        or isinstance(packet.get("inode"), bool)
        or not isinstance(packet.get("inode"), int)
    ):
        return None, "label_schema_mismatch"
    pending = runtime.store.get_by_id(pending_id)
    if pending is None or pending.source != PENDING_SOURCE or pending.status != "active":
        return None, "label_pending_missing"
    pending_payload = pending.content if isinstance(pending.content, dict) else {}
    target, reason = _target_scope(pending_payload.get("channel"), pending_payload.get("scope"), base)
    if target is None:
        return None, reason
    if (
        record.kind != "evaluation_packet"
        or record.status != "active"
        or record.source != LABEL_EVIDENCE_SOURCE
        or record.source_id != str(pending_payload.get("source_id") or "")
        or not same_scope(pending.scope, target)
        or record.meta.get("report_type") != "production_recall_label_evidence"
        or record.meta.get("authoritative") is not True
        or str(record.meta.get("operator_packet_digest") or "").lower() != packet_digest
        or [str(item) for item in record.evidence] != [pending_id, record_ref]
        or record_ref not in [str(item) for item in pending_payload.get("candidate_refs") or ()]
    ):
        return None, "label_source_mismatch"
    candidate = runtime.store.get_by_id(record_ref, scope=target)
    if candidate is None or candidate.status != "active" or candidate.source_id != record.source_id:
        return None, "label_candidate_boundary_invalid"
    expected_id = "prle_" + _stable_digest(
        {"pending_record_id": pending_id, "record_ref": record_ref, "grade": grade, "labeler": labeler}
    )[:32]
    if record.record_id != expected_id:
        return None, "label_record_identity_invalid"
    return target, ""


def _validate_accepted(
    runtime: Any,
    record: RecordEnvelope,
    base: ScopeRef,
    _unused: ScopeRef,
) -> tuple[ScopeRef | None, str]:
    payload = record.content if isinstance(record.content, dict) else {}
    case = payload.get("case") if isinstance(payload.get("case"), dict) else {}
    if payload.get("schema") != ACCEPTED_QUERY_SCHEMA or not case:
        return None, "accepted_schema_mismatch"
    target, reason = _target_scope(case.get("channel"), case.get("scope"), base)
    if target is None:
        return None, reason
    moved = RecordEnvelope.from_dict(record.to_dict())
    moved.scope = target
    validation_error = accepted_production_query_validation_error(
        runtime,
        moved,
        exact_scope=target,
        channel=str(case.get("channel") or ""),
    )
    if validation_error:
        return None, validation_error
    return target, ""


def _target_scope(channel_value: Any, embedded_scope: Any, base: ScopeRef) -> tuple[ScopeRef | None, str]:
    channel = str(channel_value or "").strip().lower()
    if channel not in SUPPORTED_RUNTIME_CHANNELS:
        return None, "repair_channel_invalid"
    target = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
    if not isinstance(embedded_scope, dict) or not same_scope(ScopeRef.from_dict(embedded_scope), target):
        return None, "embedded_scope_mismatch"
    return target, ""


def _channel_for_record(record_type: str, record: RecordEnvelope) -> str:
    payload = record.content if isinstance(record.content, dict) else {}
    if record_type == "accepted":
        case = payload.get("case") if isinstance(payload.get("case"), dict) else {}
        return str(case.get("channel") or "unknown")
    if record_type == "label":
        return "derived"
    return str(payload.get("channel") or "unknown")


def _add_conflict(
    result: dict[str, Any],
    record_type: str,
    record_id: str,
    reason: str,
    *,
    channel: str = "unknown",
) -> None:
    result["conflict_count"] += 1
    type_counts = result["by_type"].setdefault(
        record_type,
        {"scanned": 0, "repaired": 0, "already_correct": 0, "conflicts": 0},
    )
    type_counts["conflicts"] += 1
    if channel != "unknown":
        result["by_channel"].setdefault(
            channel,
            {"scanned": 0, "repaired": 0, "already_correct": 0, "conflicts": 0},
        )["conflicts"] += 1
    if len(result["conflicts"]) < _MAX_CONFLICTS:
        result["conflicts"].append({"record_type": record_type, "record_id": record_id, "reason": reason})


def _persist_receipt(runtime: Any, *, base: ScopeRef, result: dict[str, Any]) -> str:
    summary = {
        key: result[key]
        for key in (
            "schema",
            "ok",
            "scanned_count",
            "repaired_count",
            "already_correct_count",
            "conflict_count",
            "by_type",
            "by_channel",
            "repaired_record_ids",
            "conflicts",
        )
    }
    digest = sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Production query channel-scope repair receipt",
        summary="Bounded identifiers and counts for one evidence-scope repair pass.",
        content={**summary, "digest": digest, "recorded_at": now_iso()},
        source=REPAIR_SOURCE,
        source_id="production-query-authority",
        scope=base,
        status="active",
        meta={"report_type": "production_query_channel_scope_repair", "schema": REPAIR_SCHEMA, "digest": digest},
    )
    receipt.record_id = "prqr_" + digest[:32]
    if runtime.store.get_by_id(receipt.record_id, scope=base) is None:
        runtime.store.append(receipt)
    return receipt.record_id
