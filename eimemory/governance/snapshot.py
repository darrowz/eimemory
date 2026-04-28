from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from eimemory.compatibility.migration_helpers import backup_verify
from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef

SNAPSHOT_SCHEMA_VERSION = 1


def build_governance_snapshot(runtime, scope: dict | ScopeRef) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)

    memory_quality = runtime.evolution.memory_quality_report(scope=scope_payload)
    reflection_stats = runtime.evolution.reflection_stats(scope=scope_payload)

    rules = _list_all_records(runtime, kinds=["rule"], scope=scope_ref)
    source_candidates = _list_all_records(runtime, kinds=["source_candidate"], scope=scope_ref)
    unknowns = _list_all_records(runtime, kinds=["unknown"], scope=scope_ref)
    knowledge_intake = _list_knowledge_intake_records(runtime, scope=scope_ref)
    active_intake = _build_active_intake_summary(runtime, scope=scope_ref)
    source_quality = runtime.source_quality_report(scope=scope_payload)
    collection_policy = runtime.collection_policy(scope=scope_payload)
    source_expansion = runtime.latest_source_expansion(scope=scope_payload)
    daily_briefs = _list_report_records(runtime, kinds=["reflection"], scope=scope_ref, source="eimemory.daily_brief")
    rule_evolution_reports = _list_report_records(
        runtime,
        kinds=["reflection"],
        scope=scope_ref,
        source="eimemory.rule_evolution_loop",
    )
    source_discovery_records = [
        record for record in source_candidates if record.source == "eimemory.source_discovery"
    ]

    backup_reports = _collect_backup_reports(runtime.store.root)
    warnings: list[str] = []
    if not backup_reports:
        warnings.append("no_backups_found")
    for report in backup_reports:
        if not report.get("ok"):
            warnings.append(f"backup_not_verified:{report.get('path', '')}")
        warnings.extend(_warnings_from_report(report))

    health_ok = bool(backup_reports) and not warnings

    return {
        "ok": health_ok,
        "generated_at": now_iso(),
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scope": scope_payload,
        "memory_quality": memory_quality,
        "reflection_stats": reflection_stats,
        "rules": _summarize_rules(rules),
        "recall_gaps": {
            "unknown_count": len(unknowns),
            "latest": _record_to_dict(unknowns[0]) if unknowns else None,
        },
        "source_candidates": {
            "count": len(source_candidates),
            "latest": _record_to_dict(source_candidates[0]) if source_candidates else None,
            "list": [_record_to_dict(record) for record in source_candidates[:20]],
        },
        "knowledge_intake": _summarize_knowledge_intake(knowledge_intake),
        "active_intake": active_intake,
        "source_quality": source_quality,
        "source_expansion": source_expansion,
        "source_discovery": {
            "count": len(source_discovery_records),
            "needs_review_count": sum(1 for record in source_discovery_records if record.meta.get("decision") == "needs_review"),
            "latest": _record_to_dict(source_discovery_records[0]) if source_discovery_records else None,
        },
        "daily_brief": {
            "count": len(daily_briefs),
            "latest": _daily_brief_summary(daily_briefs[0]) if daily_briefs else None,
        },
        "rule_evolution": {
            "count": len(rule_evolution_reports),
            "latest": _rule_evolution_summary(rule_evolution_reports[0]) if rule_evolution_reports else None,
        },
        "collection_policy": {
            "run_now": collection_policy["run_now"],
            "pause": collection_policy["pause"],
            "lower_frequency": collection_policy["lower_frequency"],
            "gap_queries": collection_policy["gap_queries"][:20],
        },
        "backups": {
            "count": len(backup_reports),
            "latest": backup_reports[0] if backup_reports else None,
            "list": backup_reports[:20],
        },
        "health": {
            "ok": health_ok,
            "warnings": warnings,
        },
    }


def _list_all_records(
    runtime,
    *,
    kinds: list[str],
    scope: ScopeRef,
    status: str | None = None,
    page_size: int = 500,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while True:
        page = runtime.store.list_records(
            kinds=kinds,
            scope=scope,
            status=status,
            limit=page_size,
            offset=offset,
        )
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def _list_report_records(runtime, *, kinds: list[str], scope: ScopeRef, source: str) -> list[RecordEnvelope]:
    return [
        record
        for record in _list_all_records(runtime, kinds=kinds, scope=scope)
        if record.source == source
    ]


def _list_knowledge_intake_records(runtime, *, scope: ScopeRef) -> list[RecordEnvelope]:
    records_by_id: dict[str, RecordEnvelope] = {}
    for record in _list_all_records(runtime, kinds=["knowledge_candidate"], scope=scope):
        records_by_id[record.record_id] = record
    for record in _list_all_records(runtime, kinds=["source_candidate"], scope=scope):
        if _has_intake_metadata(record):
            records_by_id.setdefault(record.record_id, record)
    return sorted(records_by_id.values(), key=_record_recency_key, reverse=True)


def _has_intake_metadata(record: RecordEnvelope) -> bool:
    return bool(str((record.meta or {}).get("intake_decision") or "").strip())


def _record_recency_key(record: RecordEnvelope) -> tuple[str, str, str]:
    return (record.time.updated_at, record.time.created_at, record.record_id)


def _summarize_knowledge_intake(records: list[RecordEnvelope]) -> dict[str, Any]:
    by_source_kind: dict[str, int] = {}
    candidate_records: list[RecordEnvelope] = []
    quarantined_count = 0
    rejected_count = 0

    for record in records:
        status = str(record.status or "").strip().lower()
        if status == "candidate":
            candidate_records.append(record)
        elif status == "quarantined":
            quarantined_count += 1
        elif status == "rejected":
            rejected_count += 1

        source_kind = str((record.meta or {}).get("source_kind") or "unknown").strip() or "unknown"
        by_source_kind[source_kind] = by_source_kind.get(source_kind, 0) + 1

    return {
        "count": len(records),
        "candidate_count": len(candidate_records),
        "quarantined_count": quarantined_count,
        "rejected_count": rejected_count,
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "latest_candidate": _record_to_dict(candidate_records[0]) if candidate_records else None,
        "recent_candidates": [_record_to_dict(record) for record in candidate_records[:20]],
    }


def _summarize_rules(rules: list[RecordEnvelope]) -> dict[str, int]:
    counts = {
        "active_count": 0,
        "accepted_count": 0,
        "candidate_count": 0,
        "rejected_count": 0,
    }
    for rule in rules:
        status = str(rule.status or "").strip().lower()
        if status == "active":
            counts["active_count"] += 1
        elif status == "accepted":
            counts["accepted_count"] += 1
        elif status == "candidate":
            counts["candidate_count"] += 1
        elif status == "rejected":
            counts["rejected_count"] += 1
    counts["total_count"] = len(rules)
    return counts


def _build_active_intake_summary(runtime, *, scope: ScopeRef) -> dict[str, Any]:
    candidates = _list_all_records(runtime, kinds=["knowledge_candidate"], scope=scope)
    paper_sources = _list_all_records(runtime, kinds=["paper_source"], scope=scope)
    knowledge_pages = _list_all_records(runtime, kinds=["knowledge_page"], scope=scope)
    memories = _list_all_records(runtime, kinds=["memory"], scope=scope)
    projected_memories = [record for record in memories if _projection_type(record) == "operational_knowledge"]
    report_records = _list_active_intake_report_records(runtime, scope=scope)

    return {
        "candidate_count": len(candidates),
        "open_candidate_count": _count_status(candidates, "candidate"),
        "promoted_candidate_count": _count_status(candidates, "promoted"),
        "reviewed_candidate_count": _count_status(candidates, "reviewed"),
        "rejected_candidate_count": _count_status(candidates, "rejected"),
        "quarantined_candidate_count": _count_status(candidates, "quarantined"),
        "paper_source_count": len(paper_sources),
        "knowledge_page_count": len(knowledge_pages),
        "external_collection": {
            "latest_report": _latest_report_section(report_records, "external_collection"),
        },
        "paper_promotion": {
            "latest_report": _latest_report_section(report_records, "paper_promotion"),
        },
        "operational_projection": {
            "projected_memory_count": len(projected_memories),
            "latest_report": _latest_report_section(report_records, "operational_projection"),
            "recent_projected_memories": [
                _projected_memory_summary(record) for record in projected_memories[:10]
            ],
        },
        "recent_candidates": [_candidate_summary(record) for record in candidates[:10]],
        "recent_paper_sources": [_paper_source_summary(record) for record in paper_sources[:10]],
        "recent_knowledge_pages": [_knowledge_page_summary(record) for record in knowledge_pages[:10]],
    }


def _list_active_intake_report_records(runtime, *, scope: ScopeRef) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    for record in _list_all_records(runtime, kinds=["replay_result", "reflection", "incident"], scope=scope):
        if _record_report_payload(record):
            records.append(record)
    return sorted(records, key=_record_recency_key, reverse=True)


def _record_report_payload(record: RecordEnvelope) -> dict[str, Any]:
    for container in (record.content, record.meta, record.provenance):
        if not isinstance(container, dict):
            continue
        if any(key in container for key in ("external_collection", "paper_promotion", "operational_projection")):
            return container
        report = container.get("report")
        if isinstance(report, dict) and any(
            key in report for key in ("external_collection", "paper_promotion", "operational_projection")
        ):
            return report
    return {}


def _daily_brief_summary(record: RecordEnvelope) -> dict[str, Any]:
    brief = record.content.get("brief") if isinstance(record.content.get("brief"), dict) else {}
    delivery = record.content.get("delivery") if isinstance(record.content.get("delivery"), dict) else {}
    conversation_summary = brief.get("conversation_summary") if isinstance(brief.get("conversation_summary"), dict) else {}
    research_digest = brief.get("research_digest") if isinstance(brief.get("research_digest"), dict) else {}
    return {
        "record_id": record.record_id,
        "date": str(brief.get("date") or record.meta.get("date") or ""),
        "message_count": int(conversation_summary.get("message_count") or 0),
        "decision_count": len(brief.get("decisions") or []),
        "followup_count": len(brief.get("followups") or []),
        "research_item_count": len(research_digest.get("items") or []),
        "delivery_channel": str(delivery.get("channel") or record.meta.get("delivery_channel") or ""),
        "delivery_status": str((delivery.get("outbox") or {}).get("status") or record.meta.get("delivery_status") or ""),
        "time": asdict(record.time),
    }


def _rule_evolution_summary(record: RecordEnvelope) -> dict[str, Any]:
    report = record.content.get("report") if isinstance(record.content.get("report"), dict) else {}
    record_ids = report.get("record_ids") if isinstance(report.get("record_ids"), dict) else {}
    return {
        "record_id": record.record_id,
        "candidate_count": int(report.get("candidate_count") or record.meta.get("candidate_count") or 0),
        "promoted_count": int(report.get("promoted_count") or record.meta.get("promoted_count") or 0),
        "replay_count": int(report.get("replay_count") or record.meta.get("replay_count") or 0),
        "created_rule_count": len(record_ids.get("created_rules") or []),
        "promotion_candidate_count": len(record_ids.get("promotion_candidates") or []),
        "time": asdict(record.time),
    }


def _latest_report_section(records: list[RecordEnvelope], section: str) -> dict[str, Any] | None:
    for record in records:
        payload = _record_report_payload(record)
        value = payload.get(section)
        if isinstance(value, dict):
            return dict(value)
    return None


def _candidate_summary(record: RecordEnvelope) -> dict[str, Any]:
    source_kind = _first_text(record.meta, record.content, record.provenance, keys=("source_kind", "collector_source_kind"))
    source_uri = _first_text(
        record.meta,
        record.content,
        record.provenance,
        keys=("source_uri", "item_url", "uri", "url", "canonical_url", "paper_url"),
    )
    return {
        "record_id": record.record_id,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "source_kind": source_kind,
        "source_uri": source_uri,
        "promotion": {
            "paper_source_id": str(record.meta.get("promoted_to_paper_source_id") or ""),
            "record_ids": list(record.meta.get("promotion_record_ids") or []),
        },
        "time": asdict(record.time),
        "meta": dict(record.meta or {}),
    }


def _paper_source_summary(record: RecordEnvelope) -> dict[str, Any]:
    source_kind = _first_text(record.meta, record.content, record.provenance, keys=("source_kind",))
    source_uri = _first_text(
        record.content,
        record.meta,
        record.provenance,
        keys=("canonical_url", "paper_url", "url", "pdf_blob_ref", "doi", "arxiv_id"),
    )
    return {
        "record_id": record.record_id,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "source_kind": source_kind,
        "source_uri": source_uri,
        "time": asdict(record.time),
        "meta": dict(record.meta or {}),
    }


def _knowledge_page_summary(record: RecordEnvelope) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "page_type": _first_text(record.meta, record.content, keys=("page_type",)),
        "source_ids": list(record.meta.get("source_ids") or record.content.get("source_ids") or []),
        "time": asdict(record.time),
        "meta": dict(record.meta or {}),
    }


def _projected_memory_summary(record: RecordEnvelope) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "source_record_id": _first_text(record.meta, record.content, record.provenance, keys=("source_record_id",)),
        "source_record_kind": _first_text(record.meta, record.content, record.provenance, keys=("source_record_kind",)),
        "time": asdict(record.time),
        "meta": dict(record.meta or {}),
    }


def _projection_type(record: RecordEnvelope) -> str:
    return _first_text(record.meta, record.content, record.provenance, keys=("projection_type",)).strip().lower()


def _count_status(records: list[RecordEnvelope], status: str) -> int:
    return sum(1 for record in records if str(record.status or "").strip().lower() == status)


def _first_text(*containers: dict[str, Any], keys: tuple[str, ...]) -> str:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return ""


def _record_to_dict(record: RecordEnvelope) -> dict[str, Any]:
    return record.to_dict()


def _collect_backup_reports(root: Path) -> list[dict[str, Any]]:
    manifest_paths = sorted(
        root.rglob("*.manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    reports: list[dict[str, Any]] = []
    seen_dirs: set[Path] = set()
    for manifest_path in manifest_paths:
        backup_target = manifest_path.with_name(manifest_path.name[: -len(".manifest.json")]).resolve()
        if backup_target in seen_dirs:
            continue
        seen_dirs.add(backup_target)
        report = backup_verify(backup_target)
        report["path"] = str(backup_target)
        report["manifest_path"] = str(manifest_path)
        report["verified"] = bool(report.get("ok"))
        reports.append(report)
    return reports


def _warnings_from_report(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for error in report.get("errors") or []:
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "backup_warning")
        warnings.append(code)
    return warnings
