"""Bounded transactional recovery of production-query authority graphs."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from eimemory.core.clock import now_iso
import re
from types import SimpleNamespace
from typing import Any

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_QUERY_SCHEMA, ACCEPTED_SOURCE, LABEL_EVIDENCE_SOURCE,
    PENDING_QUERY_SCHEMA, PENDING_SOURCE,
    accepted_production_query_validation_error,
    pending_production_query_capture_validation_error,
)
from eimemory.evaluation.real_query_schema import (
    PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER,
    PRODUCTION_REAL_QUERY_TRUSTED_LABELERS, _stable_digest,
)
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id

REPAIR_SCHEMA = "production_query_channel_scope_repair.v1"
_MAX_CONFLICTS = 50
_REPORT_TYPES = (
    ("pending", "production_recall_pending_case", PENDING_SOURCE),
    ("label", "production_recall_label_evidence", LABEL_EVIDENCE_SOURCE),
    ("accepted", "production_recall_accepted_case", ACCEPTED_SOURCE),
)


class _PreflightBlocked(Exception):
    pass


def _exact_identity(record: RecordEnvelope) -> tuple[str, ...]:
    scope = record.scope
    return (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id,
            record.source_id, record.record_id)


def _checked_exact_record(sqlite: Any, record: RecordEnvelope, scope: ScopeRef) -> RecordEnvelope:
    """Require a coherent exact row and an existing payload integrity proof."""
    source_id = record.source_id
    if (not isinstance(record.record_id, str) or not record.record_id
            or not isinstance(source_id, str) or not source_id or source_id == "*"
            or normalize_source_id(source_id) != source_id
            or not same_scope(record.scope, scope)):
        raise _PreflightBlocked("record_boundary_invalid")
    exact = sqlite.get_by_exact_ref(record.record_id, scope=scope, source_id=source_id)
    if (exact is None or _exact_identity(exact) != _exact_identity(record)
            or exact.to_dict() != record.to_dict()):
        raise _PreflightBlocked("record_projection_unverifiable")
    row = sqlite.execute(
        "SELECT payload_digest FROM records WHERE record_id=? AND tenant_id=? "
        "AND agent_id=? AND workspace_id=? AND user_id=? AND source_id=?",
        (record.record_id, scope.tenant_id, scope.agent_id, scope.workspace_id,
         scope.user_id, source_id),
    ).fetchone()
    if row is None or re.fullmatch(r"[0-9a-f]{64}", str(row["payload_digest"] or "")) is None:
        raise _PreflightBlocked("record_payload_integrity_unverifiable")
    return exact


class _ExactAuthorityStore:
    """Expose only one exact boundary to the existing read-only validators."""
    def __init__(self, sqlite: Any, scope: ScopeRef, source_id: str):
        self._sqlite, self._scope, self._source_id = sqlite, scope, source_id

    def get_by_id(self, record_id: str, scope: ScopeRef | None = None):
        if scope is not None and not same_scope(scope, self._scope):
            raise _PreflightBlocked("authority_reference_boundary_invalid")
        record = self._sqlite.get_by_exact_ref(
            record_id, scope=self._scope, source_id=self._source_id)
        if record is None:
            return None
        if (record.record_id != record_id or record.source_id != self._source_id
                or not same_scope(record.scope, self._scope)):
            raise _PreflightBlocked("authority_reference_boundary_invalid")
        return _checked_exact_record(self._sqlite, record, self._scope)

    @contextmanager
    def locked(self):
        # Existing capture validation issues exact-boundary SELECTs only.
        yield self._sqlite

    def list_records_by_meta_value(self, *, scope, source_ids=None, **kwargs):
        if (not same_scope(scope, self._scope)
                or (source_ids is not None and list(source_ids) != [self._source_id])):
            raise _PreflightBlocked("authority_reference_boundary_invalid")
        records = self._sqlite.list_records_by_meta_value(
            scope=self._scope, source_ids=[self._source_id], exact_scope=True, **kwargs)
        if records is None:
            raise _PreflightBlocked("authority_scan_unavailable")
        for record in records:
            if record.source_id != self._source_id or not same_scope(record.scope, self._scope):
                raise _PreflightBlocked("authority_reference_boundary_invalid")
        return [_checked_exact_record(self._sqlite, record, self._scope) for record in records]


def _preflight_production_query_graph(sqlite, *, base, bounded, scan_bound, result, persist_receipt):
    scan_scopes = []
    for candidate in [base, *(ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
                              for channel in sorted(SUPPORTED_RUNTIME_CHANNELS))]:
        if candidate not in scan_scopes:
            scan_scopes.append(candidate)
    batches = {}
    for record_type, report_type, _source in _REPORT_TYPES:
        totals = [sqlite.count_records_by_meta_value(kinds=["evaluation_packet"],
            scope=scope, meta_key="report_type", meta_value=report_type, status="active", exact_scope=True)
            for scope in scan_scopes]
        if any(total is None for total in totals):
            raise _PreflightBlocked("indexed_record_scan_unavailable")
        total = sum(totals)
        if total > scan_bound:
            result["overflow_count"] += total - scan_bound
            _add_conflict(result, record_type, "", "indexed_record_scan_overflow")
    if result["conflict_count"]:
        raise _PreflightBlocked("indexed_record_scan_overflow")
    for record_type, report_type, expected_source in _REPORT_TYPES:
        records, seen = [], set()
        result["by_type"][record_type] = dict(scanned=0, repaired=0, already_correct=0,
                                             quarantined=0, conflicts=0)
        for scan_scope in scan_scopes:
            offset = 0
            while True:
                page_limit = min(bounded, scan_bound - len(records) + 1)
                page = sqlite.list_records_by_meta_value(
                    kinds=["evaluation_packet"], scope=scan_scope, meta_key="report_type",
                    meta_value=report_type, status="active", limit=page_limit,
                    offset=offset, exact_scope=True)
                if not isinstance(page, list) or len(page) > page_limit:
                    raise _PreflightBlocked("indexed_record_scan_unavailable")
                for record in page:
                    if (record.kind != "evaluation_packet" or record.status != "active"
                            or record.source != expected_source
                            or not isinstance(record.content, dict)
                            or not isinstance(record.meta, dict)
                            or record.meta.get("report_type") != report_type
                            or not same_scope(record.scope, scan_scope)):
                        raise _PreflightBlocked("indexed_record_boundary_invalid")
                    exact = _checked_exact_record(sqlite, record, scan_scope)
                    identity = _exact_identity(exact)
                    if identity in seen:
                        raise _PreflightBlocked("indexed_record_scan_duplicate")
                    seen.add(identity)
                    records.append(exact)
                    if len(records) > scan_bound:
                        result["overflow_count"] = len(records) - scan_bound
                        raise _PreflightBlocked("indexed_record_scan_overflow")
                offset += len(page)
                if len(page) < page_limit:
                    break
        batches[record_type] = records
        result["scanned_count"] += len(records)
        result["by_type"][record_type]["scanned"] = len(records)

    changed = []
    validators = {"pending": _validate_pending, "label": _validate_label, "accepted": _validate_accepted}
    # Reconcile only referenced gold, at each admitted exact scope, inside the
    # same writer transaction as graph recovery. Never scan unrelated tenants.
    ids = {str(record.content.get("record_ref")) for record in batches["label"]}
    for scan_scope in scan_scopes:
        fixed = sqlite.repair_status_projection_mismatches(
            scope=scan_scope, record_ids=sorted(ids), limit=scan_bound, commit=False)
        result["status_projection_repaired_count"] += fixed["repaired_count"]
        result["status_projection_repaired_record_ids"].extend(fixed["repaired_record_ids"])
        for ref in fixed.get("repaired_record_refs", []):
            changed.append(sqlite.get_by_exact_ref(ref["record_id"],
                scope=ScopeRef.from_dict(ref["scope"]), source_id=ref["source_id"]))

    # Recover parents before descendants. Each proposed move is visible only
    # inside BEGIN IMMEDIATE; failed authority validation rolls it back.
    quarantine = {}
    for record_type, _report_type, _source in _REPORT_TYPES:
        for record in batches[record_type]:
            payload = record.content
            pending = record if record_type == "pending" else None
            if record_type != "pending":
                pending_id = (payload.get("pending_record_id") if record_type == "label"
                              else (record.evidence[0] if record.evidence else ""))
                candidates = [sqlite.get_by_exact_ref(pending_id, scope=scope, source_id=record.source_id)
                              for scope in scan_scopes]
                candidates = [item for item in candidates if item is not None and item.source == PENDING_SOURCE]
                if len(candidates) != 1:
                    _add_conflict(result, record_type, record.record_id, "label_pending_missing_or_ambiguous")
                    continue
                pending = candidates[0]
            boundary = payload.get("case", {}) if record_type == "accepted" else pending.content
            target, reason = _target_scope(boundary.get("channel"), boundary.get("scope"), base)
            if target is None:
                _add_conflict(result, record_type, record.record_id, reason)
                continue
            if not (same_scope(record.scope, base) or same_scope(record.scope, target)):
                _add_conflict(result, record_type, record.record_id, "record_boundary_invalid")
                continue
            channel = str(boundary.get("channel") or "unknown")
            channel_counts = result["by_channel"].setdefault(channel,
                dict(scanned=0, repaired=0, already_correct=0, quarantined=0, conflicts=0))
            channel_counts["scanned"] += 1
            view = SimpleNamespace(store=_ExactAuthorityStore(sqlite, target, record.source_id))
            proposed = RecordEnvelope.from_dict(record.to_dict())
            proposed.scope = target
            sqlite.execute("SAVEPOINT recover_query_record")
            try:
                if not same_scope(record.scope, target):
                    occupied = sqlite.execute("SELECT 1 FROM records WHERE record_id=? AND tenant_id=? "
                        "AND agent_id=? AND workspace_id=? AND user_id=?",
                        (record.record_id, *asdict(target).values())).fetchone()
                    if occupied:
                        raise _PreflightBlocked("target_scope_collision")
                    sqlite.rewrite(proposed, previous_scope=record.scope, commit=False)
                validated, reason = validators[record_type](view, proposed, base, base)
                parent_reason = quarantine.get((pending.record_id, record.source_id), "")
                if pending.status == "quarantined" and pending.meta.get("quarantine_schema") == "production_query_authority_quarantine.v1":
                    parent_reason = pending.meta.get("quarantine_reason", "")
                if record_type == "accepted":
                    for label in payload.get("case", {}).get("labels", []):
                        evidence_id = label.get("provenance", {}).get("evidence_ref", "")
                        evidence = view.store.get_by_id(evidence_id)
                        if evidence is not None and evidence.status == "quarantined":
                            parent_reason = evidence.meta.get("quarantine_reason", "")
                allowed_reasons = {"pending_capture_decision_missing", "pending_capture_decision_mismatch",
                                   "label_candidate_boundary_invalid"}
                revoke = parent_reason if parent_reason in allowed_reasons else (reason if reason in allowed_reasons else "")
                if revoke:
                    sqlite.execute("ROLLBACK TO recover_query_record")
                    proposed = RecordEnvelope.from_dict(record.to_dict())
                    proposed.status = "quarantined"
                    proposed.meta.update(quarantine_schema="production_query_authority_quarantine.v1", quarantine_reason=revoke)
                    sqlite.upsert(proposed, commit=False)
                    quarantine[(record.record_id, record.source_id)] = revoke
                    result["quarantined_count"] += 1
                    result["by_type"][record_type]["quarantined"] += 1
                    channel_counts["quarantined"] += 1
                    result["quarantined_record_ids"].append(record.record_id)
                    result["quarantine_reasons"][revoke] = result["quarantine_reasons"].get(revoke, 0) + 1
                    changed.append(proposed)
                elif validated is None:
                    sqlite.execute("ROLLBACK TO recover_query_record")
                    _add_conflict(result, record_type, record.record_id, reason or "evidence_authority_unverifiable")
                elif not same_scope(record.scope, target):
                    result["repaired_count"] += 1
                    result["by_type"][record_type]["repaired"] += 1
                    channel_counts["repaired"] += 1
                    result["repaired_record_ids"].append(record.record_id)
                    changed.append(proposed)
                else:
                    result["already_correct_count"] += 1
                    result["by_type"][record_type]["already_correct"] += 1
                    channel_counts["already_correct"] += 1
            finally:
                sqlite.execute("RELEASE recover_query_record")
    if result["conflict_count"]:
        raise _PreflightBlocked("evidence_authority_unverifiable")
    result.update(ok=True, status="repaired", blocked_reason="")
    if persist_receipt:
        summary = {key: value for key, value in result.items() if key != "receipt_id"}
        digest = _stable_digest(summary)
        receipt = RecordEnvelope.create(kind="evaluation_packet",
            title="Production query channel-scope repair receipt",
            summary="Bounded identifiers and counts for one evidence-scope repair pass.",
            content={**summary, "digest": digest, "recorded_at": now_iso()},
            source="eimemory.production_recall.scope_repair", source_id="production-query-authority",
            scope=base, meta={"report_type": "production_query_channel_scope_repair", "schema": REPAIR_SCHEMA, "digest": digest})
        receipt.record_id = "prqr_" + digest[:32]
        if sqlite.get_by_exact_ref(receipt.record_id, scope=base, source_id=receipt.source_id) is None:
            sqlite.upsert(receipt, commit=False)
            changed.append(receipt)
        result["receipt_id"] = receipt.record_id
    return result, changed, []


def repair_production_query_channel_scopes(
    runtime: Any, *, scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500, persist_receipt: bool = True, complete_scan: bool = False,
) -> dict[str, Any]:
    """Recover authorized channel evidence in one SQLite writer transaction.

    Existing capture and signed-label validators remain the authority. A
    conflict rolls back the entire graph, including status projection changes.
    """
    result = {
        "schema": REPAIR_SCHEMA, "ok": False, "status": "blocked", "read_only": False,
        "scanned_count": 0, "repaired_count": 0, "already_correct_count": 0,
        "quarantined_count": 0, "conflict_count": 0, "overflow_count": 0,
        "by_type": {}, "by_channel": {}, "repaired_record_ids": [],
        "quarantined_record_ids": [], "quarantine_reasons": {}, "conflicts": [],
        "status_projection_repaired_count": 0, "status_projection_repaired_record_ids": [],
        "receipt_id": "",
    }
    try:
        if scope is None:
            raise _PreflightBlocked("exact_base_scope_required")
        if type(limit) is not int or limit < 1 or type(complete_scan) is not bool or type(persist_receipt) is not bool:
            raise _PreflightBlocked("preflight_limit_invalid")
        base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        if any(not isinstance(value, str) or value == "*" for value in asdict(base).values()):
            raise _PreflightBlocked("exact_base_scope_required")
        reader = getattr(runtime.store, "mutate_records_atomically", None)
        if not callable(reader):
            raise _PreflightBlocked("authority_snapshot_unavailable")
        bounded = min(500, limit)
        completed = reader(lambda sqlite: _preflight_production_query_graph(
            sqlite, base=base, bounded=bounded,
            scan_bound=10000 if complete_scan else bounded, result=result, persist_receipt=persist_receipt))
        if completed is not result:
            raise _PreflightBlocked("authority_snapshot_unavailable")
    except _PreflightBlocked as exc:
        result.update(ok=False, status="blocked", blocked_reason=str(exc))
    except Exception:
        result.update(ok=False, status="blocked", blocked_reason="authority_preflight_unavailable")
    else:
        result.update(ok=True, status="repaired", blocked_reason="")
    if not result["ok"]:
        result.update(repaired_count=0, quarantined_count=0, status_projection_repaired_count=0,
                      repaired_record_ids=[], quarantined_record_ids=[], status_projection_repaired_record_ids=[])
        result.update(receipt_id="", quarantine_reasons={})
        for counters in [*result["by_type"].values(), *result["by_channel"].values()]:
            counters.update(repaired=0, quarantined=0)
        if not result["conflict_count"]:
            _add_conflict(result, "preflight", "", result["blocked_reason"])
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
    if not source_id or source_id != record.source_id or not isinstance(refs, list) or not 0 <= len(refs) <= 5:
        return None, "pending_source_or_refs_invalid"
    # Returned refs describe a historical observation. Their scope/source and
    # order are verified against the immutable decision below, not current
    # memory lifecycle. Only relevance gold must remain active and authorized.
    capture_error = pending_production_query_capture_validation_error(
        runtime,
        record,
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
    if labeler == PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER:
        return _validate_auto_review_label(runtime, record, base, payload)
    operator_fields = {
        "evidence_class",
        "labeler",
        "pending_record_id",
        "record_ref",
        "grade",
        "operator_packet_evidence",
    }
    if (
        # Signed operator labels (1.13.25+) also carry ``operator_authority``;
        # its HMAC is verified by label_authority_error below.
        set(payload) not in (operator_fields, operator_fields | {"operator_authority"})
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
    from .label_authority import label_authority_error
    label_error = label_authority_error(record, scope=target, source_id=record.source_id,
        pending_id=pending_id, record_ref=record_ref, grade=grade, labeler=labeler)
    if label_error:
        return None, label_error
    return target, ""



def _validate_auto_review_label(
    runtime: Any,
    record: RecordEnvelope,
    base: ScopeRef,
    payload: dict[str, Any],
) -> tuple[ScopeRef | None, str]:
    """Auto-reviewed labels share the label graph but carry a signed review packet."""

    pending_id = str(payload.get("pending_record_id") or "")
    record_ref = str(payload.get("record_ref") or "")
    grade = payload.get("grade")
    labeler = str(payload.get("labeler") or "")
    if (
        set(payload) != {
            "evidence_class", "labeler", "pending_record_id", "record_ref", "grade",
            "auto_review_packet", "auto_review_authority",
        }
        or payload.get("evidence_class") != "auto_review_relevance_label"
        or not pending_id
        or not record_ref
        or isinstance(grade, bool)
        or not isinstance(grade, int)
        or not 1 <= grade <= 3
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
        or record.meta.get("label_authority") != "auto_review"
        or [str(item) for item in record.evidence] != [pending_id, record_ref]
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
    from .label_authority import label_authority_error
    label_error = label_authority_error(record, scope=target, source_id=record.source_id,
        pending_id=pending_id, record_ref=record_ref, grade=grade, labeler=labeler)
    if label_error:
        return None, label_error
    from .production_query_auto_review import auto_review_revocation_reason
    revocation_reason = auto_review_revocation_reason(
        runtime, pending_id=pending_id, scope=target)
    if revocation_reason:
        return None, revocation_reason
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
    validation_error = accepted_production_query_validation_error(
        runtime,
        record,
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
        {
            "scanned": 0,
            "repaired": 0,
            "already_correct": 0,
            "quarantined": 0,
            "conflicts": 0,
        },
    )
    type_counts["conflicts"] += 1
    if channel != "unknown":
        result["by_channel"].setdefault(
            channel,
            {
                "scanned": 0,
                "repaired": 0,
                "already_correct": 0,
                "quarantined": 0,
                "conflicts": 0,
            },
        )["conflicts"] += 1
    if len(result["conflicts"]) < _MAX_CONFLICTS:
        result["conflicts"].append({"record_type": record_type, "record_id": record_id, "reason": reason})
