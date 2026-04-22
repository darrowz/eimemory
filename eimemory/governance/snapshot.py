from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from eimemory.compatibility.migration_helpers import backup_verify
from eimemory.models.records import RecordEnvelope, ScopeRef


def build_governance_snapshot(runtime, scope: dict | ScopeRef) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)

    memory_quality = runtime.evolution.memory_quality_report(scope=scope_payload)
    reflection_stats = runtime.evolution.reflection_stats(scope=scope_payload)

    rules = _list_all_records(runtime, kinds=["rule"], scope=scope_ref)
    source_candidates = _list_all_records(runtime, kinds=["source_candidate"], scope=scope_ref)
    unknowns = _list_all_records(runtime, kinds=["unknown"], scope=scope_ref)
    knowledge_intake = _list_knowledge_intake_records(runtime, scope=scope_ref)
    source_quality = runtime.source_quality_report(scope=scope_payload)
    collection_policy = runtime.collection_policy(scope=scope_payload)

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
        "source_quality": source_quality,
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
