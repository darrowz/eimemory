from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef

_SOURCE_RECORD_KINDS = ["knowledge_candidate", "source_candidate", "memory"]
_UNKNOWN_RECORD_KINDS = ["unknown", "reflection", "recall_view"]
_PAGE_SIZE = 500


def build_source_quality_report(runtime: Any, scope: dict[str, Any] | ScopeRef) -> dict[str, Any]:
    """Summarize intake outcomes by source kind and source id."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    by_source: dict[str, dict[str, dict[str, Any]]] = {}

    for record in _list_all_records(runtime, kinds=_SOURCE_RECORD_KINDS, scope=scope_ref):
        source_kind, source_id = _source_ref(record)
        if not source_id:
            continue
        entry = by_source.setdefault(source_kind, {}).setdefault(source_id, _empty_entry(source_kind, source_id))
        bucket = _outcome_bucket(record)
        if bucket:
            entry[f"{bucket}_count"] += 1
        score = _quality_score(record)
        if score is not None:
            entry["_score_total"] += score
            entry["_score_count"] += 1
        entry["last_seen"] = max(str(entry["last_seen"] or ""), _last_seen(record))

    sources: list[dict[str, Any]] = []
    for source_kind in sorted(by_source):
        for source_id in sorted(by_source[source_kind]):
            entry = by_source[source_kind][source_id]
            score_count = int(entry.pop("_score_count"))
            score_total = float(entry.pop("_score_total"))
            entry["avg_quality_score"] = round(score_total / score_count, 3) if score_count else None
            sources.append(dict(entry))

    return {
        "scope": asdict(scope_ref),
        "source_count": len(sources),
        "by_source": by_source,
        "sources": sources,
    }


def recommend_collection_policy(
    runtime: Any,
    scope: dict[str, Any] | ScopeRef,
    topic_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """Return next-round collection recommendations without executing collection."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    report = build_source_quality_report(runtime, scope_ref)
    run_now: list[str] = []
    pause: list[str] = []
    lower_frequency: list[str] = []

    for item in report["sources"]:
        source_id = str(item["source_id"])
        quarantined = int(item["quarantined_count"])
        rejected = int(item["rejected_count"])
        promoted = int(item["promoted_count"])
        avg_score = item["avg_quality_score"]

        if quarantined >= 2 or (quarantined > promoted and quarantined >= 1 and rejected >= 1):
            pause.append(source_id)
            continue
        if rejected >= 2 and promoted == 0:
            lower_frequency.append(source_id)
            continue
        if promoted > 0 or (avg_score is not None and float(avg_score) >= 0.8):
            run_now.append(source_id)

    return {
        "scope": asdict(scope_ref),
        "run_now": sorted(set(run_now)),
        "pause": sorted(set(pause)),
        "lower_frequency": sorted(set(lower_frequency)),
        "gap_queries": _gap_queries(runtime, scope_ref, topic_gaps or []),
        "source_quality_report": report,
    }


def _empty_entry(source_kind: str, source_id: str) -> dict[str, Any]:
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "candidate_count": 0,
        "promoted_count": 0,
        "rejected_count": 0,
        "quarantined_count": 0,
        "avg_quality_score": None,
        "last_seen": "",
        "_score_total": 0.0,
        "_score_count": 0,
    }


def _list_all_records(runtime: Any, *, kinds: list[str], scope: ScopeRef) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=kinds, scope=scope, limit=_PAGE_SIZE, offset=offset)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def _source_ref(record: RecordEnvelope) -> tuple[str, str]:
    source_kind = _first_text(record.meta, record.content, record.provenance, key="source_kind")
    source_id = _first_text(record.meta, record.content, record.provenance, key="source_id")
    if not source_id:
        source_id = _first_text(record.meta, record.content, record.provenance, key="paper_source_id")
        if source_id and not source_kind:
            source_kind = "paper"
    return (source_kind or "unknown", source_id)


def _first_text(*payloads: dict[str, Any], key: str) -> str:
    for payload in payloads:
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _outcome_bucket(record: RecordEnvelope) -> str:
    status = str(record.status or "").strip().lower()
    decision = str((record.meta or {}).get("intake_decision") or "").strip().lower()
    if record.kind == "memory" and status != "rejected":
        return "promoted"
    if status == "promoted":
        return "promoted"
    if status == "quarantined" or decision in {"quarantine", "quarantined"}:
        return "quarantined"
    if status == "rejected" or decision in {"reject", "rejected"}:
        return "rejected"
    if status in {"candidate", "reviewed", "active"} or decision in {"accept", "accepted", "candidate"}:
        return "candidate"
    return ""


def _quality_score(record: RecordEnvelope) -> float | None:
    quality = record.meta.get("quality") if isinstance(record.meta, dict) else None
    if not isinstance(quality, dict):
        quality = record.content.get("quality") if isinstance(record.content, dict) else None
    if not isinstance(quality, dict):
        return None
    for key in ("score", "salience_score", "confidence"):
        if key not in quality:
            continue
        try:
            return float(quality[key])
        except (TypeError, ValueError):
            continue
    return None


def _last_seen(record: RecordEnvelope) -> str:
    return max(
        str(record.time.updated_at or ""),
        str(record.time.created_at or ""),
        str(record.time.occurred_at or ""),
    )


def _gap_queries(runtime: Any, scope: ScopeRef, topic_gaps: list[str]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for query in topic_gaps:
        _append_query(queries, seen, query)
    for record in _list_all_records(runtime, kinds=_UNKNOWN_RECORD_KINDS, scope=scope):
        _append_query(queries, seen, record.title or record.summary or record.detail)
    return queries


def _append_query(queries: list[str], seen: set[str], value: str) -> None:
    query = " ".join(str(value or "").split())
    if not query or query in seen:
        return
    seen.add(query)
    queries.append(query)
