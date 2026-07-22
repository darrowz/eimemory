"""Self-accumulating, human-labelled production recall dataset workflow."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.core.clock import now_iso
from eimemory.evaluation.real_query_gate import (
    PRODUCTION_REAL_QUERY_SCHEMA,
    PRODUCTION_REAL_QUERY_TRUSTED_LABELERS,
    _bounded_query_features,
    _stable_digest,
)
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef


PENDING_QUERY_SCHEMA = "production_recall_pending_case.v1"
ACCEPTED_QUERY_SCHEMA = "production_recall_accepted_case.v1"
PENDING_SOURCE = "eimemory.production_recall.pending_case"
ACCEPTED_SOURCE = "eimemory.production_recall.accepted_case"
LABEL_EVIDENCE_SOURCE = "eimemory.production_recall.label_evidence"


def collect_pending_production_queries(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500,
) -> dict[str, Any]:
    """Project real proactive audits into digest-only pending label cases."""

    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    rows: list[dict[str, Any]] = []
    bounded = max(1, min(500, int(limit)))
    lock = getattr(runtime.store, "_lock", None)
    sqlite = getattr(runtime.store, "sqlite", None)
    if lock is None or sqlite is None:
        return {"ok": False, "reason": "proactive_audit_store_unavailable", "created": 0, "pending_record_ids": []}
    with lock:
        for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
            exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
            selected = sqlite.conn.execute(
                "SELECT d.decision_id,d.channel,d.query_digest,d.task_type,d.source_ids_json,d.created_at,"
                "i.record_id,i.source_id FROM proactive_decisions d "
                "JOIN proactive_decision_items i ON i.decision_id=d.decision_id "
                "WHERE d.channel=? AND d.tenant_id=? AND d.agent_id=? AND d.workspace_id=? AND d.user_id=? "
                "AND d.release_bound=1 AND d.control_cohort=0 "
                "ORDER BY d.created_at DESC,d.decision_id DESC,i.item_order ASC LIMIT ?",
                (channel, exact.tenant_id, exact.agent_id, exact.workspace_id, exact.user_id, bounded),
            ).fetchall()
            rows.extend(dict(row) for row in selected)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("decision_id") or ""), []).append(row)
    created: list[str] = []
    skipped: dict[str, int] = {}
    for decision_id, items in grouped.items():
        first = items[0]
        channel = str(first.get("channel") or "")
        try:
            sources = [str(item) for item in json.loads(str(first.get("source_ids_json") or "[]"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            sources = []
        if len(sources) != 1 or sources[0] == "*":
            skipped["non_exact_source"] = skipped.get("non_exact_source", 0) + 1
            continue
        source_id = sources[0]
        query_digest = str(first.get("query_digest") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", query_digest):
            skipped["query_digest_invalid"] = skipped.get("query_digest_invalid", 0) + 1
            continue
        exact_scope = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
        refs: list[str] = []
        valid = True
        for item in items[:5]:
            if str(item.get("source_id") or "") != source_id:
                valid = False
                break
            record = runtime.store.get_by_id(str(item.get("record_id") or ""), scope=exact_scope)
            if record is None or record.status != "active" or record.source_id != source_id or not same_scope(record.scope, exact_scope):
                valid = False
                break
            if record.record_id not in refs:
                refs.append(record.record_id)
        if not valid or not refs:
            skipped["candidate_boundary_invalid"] = skipped.get("candidate_boundary_invalid", 0) + 1
            continue
        task_type = str(first.get("task_type") or "memory.recall")[:80]
        record_id = "prqp_" + _stable_digest({"schema": PENDING_QUERY_SCHEMA, "decision_id": decision_id, "query_digest": query_digest})[:32]
        pending = RecordEnvelope.create(
            kind="evaluation_packet",
            title=f"Pending production recall label {channel}",
            summary="Digest-only real proactive query awaiting operator relevance labels.",
            content={
                "schema": PENDING_QUERY_SCHEMA,
                "case_id": "real-" + _stable_digest({"decision_id": decision_id, "query_digest": query_digest})[:24],
                "channel": channel,
                "source_id": source_id,
                "scope": asdict(exact_scope),
                "capture_query_digest": query_digest,
                "suggested_query_features": {
                    "terms": [term for term in re.split(r"[^a-zA-Z0-9_.-]+", task_type) if term][:8] or ["memory", "recall"],
                    "intent": "production recall",
                },
                "candidate_refs": refs,
                "capture_ref": decision_id,
                "captured_at": str(first.get("created_at") or now_iso())[:80],
                "collector": "proactive_audit_capture",
            },
            source=PENDING_SOURCE,
            source_id=source_id,
            scope=exact_scope,
            status="active",
            evidence=refs,
            meta={
                "report_type": "production_recall_pending_case",
                "schema": PENDING_QUERY_SCHEMA,
                "channel": channel,
                "capture_ref": decision_id,
                "query_digest": query_digest,
            },
        )
        pending.record_id = record_id
        if runtime.store.get_by_id(record_id, scope=exact_scope) is None:
            runtime.store.append(pending)
            created.append(record_id)
        else:
            created.append(record_id)
    return {
        "ok": True,
        "created": len(created),
        "pending_record_ids": sorted(created),
        "skipped": dict(sorted(skipped.items())),
    }


def accept_pending_production_query(
    runtime: Any,
    *,
    pending_record_id: str,
    query_features: dict[str, Any],
    labels: list[dict[str, Any]],
    labeler: str,
    operator_scope: dict[str, Any] | ScopeRef | None,
    label_packet_evidence: dict[str, Any],
) -> dict[str, Any]:
    labeler_id = str(labeler or "").strip()
    if labeler_id not in PRODUCTION_REAL_QUERY_TRUSTED_LABELERS:
        raise ValueError("trusted operator labeler required")
    pending = runtime.store.get_by_id(str(pending_record_id or ""))
    if pending is None or pending.kind != "evaluation_packet" or pending.source != PENDING_SOURCE or pending.status != "active":
        raise ValueError("trusted pending production query required")
    payload = pending.content if isinstance(pending.content, dict) else {}
    if payload.get("schema") != PENDING_QUERY_SCHEMA:
        raise ValueError("pending production query schema mismatch")
    bounded_features, reason = _bounded_query_features(query_features)
    if reason:
        raise ValueError(reason)
    source_id = str(payload.get("source_id") or "")
    channel = str(payload.get("channel") or "")
    exact_scope = ScopeRef.from_dict(payload.get("scope") or {})
    base_scope = operator_scope if isinstance(operator_scope, ScopeRef) else ScopeRef.from_dict(operator_scope)
    authorized_scope = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base_scope)))
    evidence_digest = str(label_packet_evidence.get("digest") or label_packet_evidence.get("sha256") or "").lower()
    if not (
        source_id
        and same_scope(pending.scope, exact_scope)
        and same_scope(exact_scope, authorized_scope)
        and label_packet_evidence.get("schema") == "secure_dataset_fingerprint.v1"
        and re.fullmatch(r"[0-9a-f]{64}", evidence_digest)
        and isinstance(label_packet_evidence.get("size"), int)
        and int(label_packet_evidence.get("size") or 0) > 0
        and isinstance(label_packet_evidence.get("device"), int)
        and isinstance(label_packet_evidence.get("inode"), int)
    ):
        raise ValueError("pending query boundary mismatch")
    normalized_labels: list[dict[str, Any]] = []
    seen: set[str] = set()
    accepted_at = now_iso()
    for raw in labels[:16]:
        ref = str(raw.get("record_ref") or "") if isinstance(raw, dict) else ""
        grade = raw.get("grade") if isinstance(raw, dict) else None
        if not ref or ref in seen or isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3:
            raise ValueError("operator label invalid")
        record = runtime.store.get_by_id(ref, scope=exact_scope)
        if record is None or record.status != "active" or record.source_id != source_id or not same_scope(record.scope, exact_scope):
            raise ValueError("operator label boundary mismatch")
        seen.add(ref)
        evidence_id = "prle_" + _stable_digest(
            {"pending_record_id": pending.record_id, "record_ref": ref, "grade": grade, "labeler": labeler_id}
        )[:32]
        evidence = RecordEnvelope.create(
            kind="evaluation_packet",
            title=f"Trusted production recall label {channel}",
            summary="Operator accepted one exact relevance label.",
            content={
                "evidence_class": "operator_relevance_label",
                "labeler": labeler_id,
                "pending_record_id": pending.record_id,
                "record_ref": ref,
                "grade": grade,
                "operator_packet_evidence": {
                    "schema": "secure_dataset_fingerprint.v1",
                    "digest": evidence_digest,
                    "size": int(label_packet_evidence["size"]),
                    "device": int(label_packet_evidence["device"]),
                    "inode": int(label_packet_evidence["inode"]),
                },
            },
            source=LABEL_EVIDENCE_SOURCE,
            source_id=source_id,
            scope=exact_scope,
            status="active",
            evidence=[pending.record_id, ref],
            meta={
                "report_type": "production_recall_label_evidence",
                "authoritative": True,
                "operator_packet_digest": evidence_digest,
            },
        )
        evidence.record_id = evidence_id
        if runtime.store.get_by_id(evidence_id, scope=exact_scope) is None:
            runtime.store.append(evidence)
        normalized_labels.append(
            {
                "record_ref": ref,
                "grade": grade,
                "accepted": True,
                "provenance": {"labeler": labeler_id, "labelled_at": accepted_at, "evidence_ref": evidence_id},
            }
        )
    if not normalized_labels:
        raise ValueError("at least one operator label required")
    started_at = str(payload.get("captured_at") or accepted_at)
    try:
        if datetime.fromisoformat(started_at.replace("Z", "+00:00")) >= datetime.fromisoformat(accepted_at.replace("Z", "+00:00")):
            started_at = (datetime.fromisoformat(accepted_at) - timedelta(seconds=1)).isoformat()
    except ValueError:
        started_at = (datetime.fromisoformat(accepted_at) - timedelta(seconds=1)).isoformat()
    case = {
        "case_id": str(payload.get("case_id") or ""),
        "collection_window": {"started_at": started_at, "ended_at": accepted_at},
        "channel": channel,
        "source_id": source_id,
        "scope": asdict(exact_scope),
        "query_features": bounded_features,
        "query_digest": _stable_digest(bounded_features),
        "labels": normalized_labels,
        "provenance": {"collector": "proactive_audit_capture", "capture_ref": str(payload.get("capture_ref") or pending.record_id)},
    }
    accepted_id = "prqa_" + _stable_digest({"schema": ACCEPTED_QUERY_SCHEMA, "case": case})[:32]
    accepted = RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Accepted production recall case {channel}",
        summary="Human-labelled redacted production recall case.",
        content={"schema": ACCEPTED_QUERY_SCHEMA, "case": case},
        source=ACCEPTED_SOURCE,
        source_id=source_id,
        scope=exact_scope,
        status="active",
        evidence=[pending.record_id, *[item["record_ref"] for item in normalized_labels]],
        meta={"report_type": "production_recall_accepted_case", "schema": ACCEPTED_QUERY_SCHEMA, "channel": channel, "case_id": case["case_id"]},
    )
    accepted.record_id = accepted_id
    if runtime.store.get_by_id(accepted_id, scope=exact_scope) is None:
        runtime.store.append(accepted)
    return {"ok": True, "record_id": accepted_id, "case_id": case["case_id"], "channel": channel}


def build_production_query_dataset(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500,
) -> dict[str, Any]:
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    cases: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
        records = runtime.store.list_records(kinds=["evaluation_packet"], scope=exact, status="active", limit=max(1, min(500, int(limit))))
        for record in records:
            if record.source != ACCEPTED_SOURCE or not same_scope(record.scope, exact):
                continue
            case = record.content.get("case") if isinstance(record.content, dict) and isinstance(record.content.get("case"), dict) else {}
            case_id = str(case.get("case_id") or "")
            if case_id in seen or str(case.get("channel") or "") != channel:
                continue
            seen.add(case_id)
            cases.append(dict(case))
            counts[channel] = counts.get(channel, 0) + 1
    counts = {channel: counts.get(channel, 0) for channel in sorted(SUPPORTED_RUNTIME_CHANNELS)}
    ready = all(count >= 5 for count in counts.values())
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-real-query",
        "dataset_kind": "production",
        "scope": asdict(base),
        "cases": sorted(cases, key=lambda item: str(item.get("case_id") or "")),
        "baseline_report_id": "",
    }
    return {
        "ok": True,
        "ready": ready,
        "progress": {
            "accepted_case_count": len(cases),
            "required_case_count": 15,
            "required_per_channel": 5,
            "per_channel_accepted": counts,
        },
        "dataset": dataset,
    }


def write_production_query_dataset(dataset: dict[str, Any], path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise ValueError("production recall dataset target must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    digest = sha256(raw).hexdigest()
    if target.exists():
        existing = target.read_bytes()
        if existing != raw:
            raise FileExistsError("immutable production recall dataset already exists with different content")
        return {"ok": True, "path": str(target), "digest": digest, "size": len(raw), "unchanged": True}
    temporary = target.with_name(f".{target.name}.{digest[:16]}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"ok": True, "path": str(target), "digest": digest, "size": len(raw), "unchanged": False}
