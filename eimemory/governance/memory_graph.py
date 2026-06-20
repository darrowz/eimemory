from __future__ import annotations

import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.memory_edges import MEMORY_EDGE_TYPES, MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


GRAPH_RECORD_KINDS = [
    "memory",
    "reflection",
    "rule",
    "replay_result",
    "knowledge_page",
    "claim_card",
    "knowledge_candidate",
    "news",
    "world_signal",
    "learning_goal",
    "capability_candidate",
    "promotion_request",
]
_GRAPH_CURSOR_TITLE = "Memory graph edge cursor"
_STOP_TERMS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "when",
    "what",
    "why",
    "how",
    "memory",
    "record",
    "summary",
}


def build_incremental_memory_edges(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 24,
    dry_run: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    cursor = _load_graph_cursor(runtime, scope=scope_ref)
    records = [
        record
        for record in runtime.store.list_records(kinds=GRAPH_RECORD_KINDS, scope=scope_ref, limit=max(20, int(limit)) * 4)
        if _record_after_cursor(record, cursor) and _is_graph_record_candidate(record)
    ]
    records.sort(key=lambda item: (_record_time(item), item.record_id))
    records = records[: max(0, int(limit))]
    reference_limit = max(60, min(160, max(1, int(limit)) * 5))
    references = [
        record
        for record in runtime.store.list_records(kinds=GRAPH_RECORD_KINDS, scope=scope_ref, limit=reference_limit)
        if _is_graph_record_candidate(record)
    ]
    by_id = {record.record_id: record for record in references}
    edges: list[MemoryEdge] = []
    for record in records:
        candidates = [item for item in references if item.record_id != record.record_id]
        edges.extend(_semantic_edges(record, candidates, scope=scope_ref))
        edges.extend(_temporal_edges(record, candidates, scope=scope_ref))
        edges.extend(_causal_edges(record, by_id, scope=scope_ref))
        edges.extend(_entity_edges(record, candidates, scope=scope_ref))
    unique_edges = _dedupe_edges(edges)
    if not dry_run:
        bulk_upsert = getattr(runtime.store, "upsert_memory_edges", None)
        if callable(bulk_upsert):
            bulk_upsert(unique_edges)
        else:
            for edge in unique_edges:
                runtime.store.upsert_memory_edge(edge)
        if records:
            _save_graph_cursor(runtime, scope=scope_ref, records=records)
    edge_counts: dict[str, int] = {}
    for edge in unique_edges:
        edge_counts[edge.edge_type] = edge_counts.get(edge.edge_type, 0) + 1
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "scanned_count": len(records),
        "edge_count": len(unique_edges),
        "edge_counts": edge_counts,
        "batch_limit": max(0, int(limit)),
        "reference_limit": reference_limit,
        "last_seen": cursor.get("last_seen", ""),
        "high_watermark": _high_watermark(records),
        "cursor_record_ids": [record.record_id for record in records if _record_time(record) == _high_watermark(records)],
    }


def graph_route_for_query(query: str, *, intent_name: str = "", task_context: dict[str, Any] | None = None) -> dict[str, Any]:
    text = " ".join([str(query or ""), str(intent_name or ""), " ".join(str(v) for v in dict(task_context or {}).values())]).lower()
    route: list[str] = []
    reasons: list[str] = []
    if _has_any(text, ("昨天", "之前", "以后", "后来", "时间", "版本", "升级", "commit", "release", "latest", "recent", "before", "after", "when")):
        route.append("temporal")
        reasons.append("temporal_question")
    if _has_any(text, ("为什么", "为何", "原因", "根因", "失败", "怎么改进", "because", "why", "failed", "failure", "root cause", "improve")):
        route.append("causal")
        reasons.append("causal_question")
    if _has_entity_like_query(text):
        route.append("entity")
        reasons.append("entity_anchor")
    if not route or "semantic" not in route:
        route.append("semantic")
    ordered = [edge_type for edge_type in ("temporal", "causal", "entity", "semantic") if edge_type in route]
    return {
        "edge_types": ordered,
        "primary": ordered[0] if ordered else "semantic",
        "reasons": reasons or ["semantic_default"],
    }


def build_evidence_refs(records: list[RecordEnvelope], edges: list[MemoryEdge] | None = None) -> list[dict[str, Any]]:
    linked_edges = list(edges or [])
    refs: list[dict[str, Any]] = []
    for record in records:
        meta = business_metadata(record.meta)
        containers = [record.content if isinstance(record.content, dict) else {}, record.provenance, meta]
        refs.append(
            {
                "source_record_id": record.record_id,
                "record_id": record.record_id,
                "kind": record.kind,
                "title": record.title,
                "source": record.source,
                "created_at": record.time.created_at,
                "updated_at": record.time.updated_at,
                "occurred_at": record.time.occurred_at,
                "evidence_ids": list(record.evidence or []),
                "edge_ids": [
                    edge.edge_id
                    for edge in linked_edges
                    if edge.from_id == record.record_id or edge.to_id == record.record_id
                ],
                "commit_sha": _first_value(containers, ("commit_sha", "commit", "release_commit")),
                "release_path": _first_value(containers, ("release_path", "release")),
                "ledger_id": _first_value(containers, ("ledger_id", "rollout_ledger_id", "promotion_id")),
                "event_id": _first_value(containers, ("event_id", "source_event_id")),
                "outcome_id": _first_value(containers, ("outcome_id", "source_outcome_id")),
            }
        )
    return refs


def build_timeline(records: list[RecordEnvelope]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": record.record_id,
            "kind": record.kind,
            "title": record.title,
            "occurred_at": record.time.occurred_at,
            "updated_at": record.time.updated_at,
        }
        for record in sorted(records, key=lambda item: (_record_time(item), item.record_id))
    ]


def _semantic_edges(record: RecordEnvelope, candidates: list[RecordEnvelope], *, scope: ScopeRef) -> list[MemoryEdge]:
    record_terms = set(_terms(_record_text(record)))
    if not record_terms:
        return []
    ranked: list[tuple[float, RecordEnvelope]] = []
    for candidate in candidates:
        candidate_terms = set(_terms(_record_text(candidate)))
        if not candidate_terms:
            continue
        overlap = record_terms & candidate_terms
        if not overlap:
            continue
        score = len(overlap) / max(1, len(record_terms | candidate_terms))
        if score >= 0.18:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (-item[0], _record_time(item[1])))
    return [
        MemoryEdge.create(
            from_id=record.record_id,
            to_id=candidate.record_id,
            edge_type="semantic",
            confidence=score,
            evidence_id=record.record_id,
            scope=scope,
            reason="term_overlap",
            meta={"shared_terms": sorted(set(_terms(_record_text(record))) & set(_terms(_record_text(candidate))))[:12]},
        )
        for score, candidate in ranked[:2]
    ]


def _temporal_edges(record: RecordEnvelope, candidates: list[RecordEnvelope], *, scope: ScopeRef) -> list[MemoryEdge]:
    current_time = _record_time(record)
    previous = [
        candidate
        for candidate in candidates
        if _record_time(candidate) and _record_time(candidate) <= current_time
    ]
    if not previous:
        return []
    previous.sort(key=lambda item: (_record_time(item), item.record_id), reverse=True)
    candidate = previous[0]
    return [
        MemoryEdge.create(
            from_id=candidate.record_id,
            to_id=record.record_id,
            edge_type="temporal",
            confidence=0.65,
            evidence_id=record.record_id,
            scope=scope,
            reason="previous_record_in_scope",
        )
    ]


def _causal_edges(record: RecordEnvelope, by_id: dict[str, RecordEnvelope], *, scope: ScopeRef) -> list[MemoryEdge]:
    causal_ids = _causal_source_ids(record)
    edges: list[MemoryEdge] = []
    for causal_id in causal_ids:
        if causal_id == record.record_id or causal_id not in by_id:
            continue
        edges.append(
            MemoryEdge.create(
                from_id=causal_id,
                to_id=record.record_id,
                edge_type="causal",
                confidence=0.82,
                evidence_id=record.record_id,
                scope=scope,
                reason="explicit_causal_reference",
            )
        )
    return edges


def _entity_edges(record: RecordEnvelope, candidates: list[RecordEnvelope], *, scope: ScopeRef) -> list[MemoryEdge]:
    anchors = set(_entity_anchors(record))
    if not anchors:
        return []
    ranked: list[tuple[int, RecordEnvelope, set[str]]] = []
    for candidate in candidates:
        shared = anchors & set(_entity_anchors(candidate))
        if shared:
            ranked.append((len(shared), candidate, shared))
    ranked.sort(key=lambda item: (-item[0], _record_time(item[1])))
    edges: list[MemoryEdge] = []
    for count, candidate, shared in ranked[:3]:
        edges.append(
            MemoryEdge.create(
                from_id=record.record_id,
                to_id=candidate.record_id,
                edge_type="entity",
                confidence=min(0.9, 0.45 + 0.08 * count),
                evidence_id=record.record_id,
                scope=scope,
                reason="shared_entity_anchor",
                meta={"anchors": sorted(shared)[:12]},
            )
        )
    return edges


def _causal_source_ids(record: RecordEnvelope) -> list[str]:
    ids: list[str] = []
    containers = [record.content if isinstance(record.content, dict) else {}, record.provenance, business_metadata(record.meta)]
    for container in containers:
        for key in ("cause_record_id", "caused_by_record_id", "root_cause_record_id"):
            value = container.get(key)
            if value:
                ids.append(str(value))
        for key in ("cause_record_ids", "caused_by_record_ids", "source_record_ids"):
            value = container.get(key)
            if isinstance(value, (list, tuple)):
                ids.extend(str(item) for item in value if str(item))
    for link in record.links:
        relation = str(link.relation or "").lower()
        if "cause" in relation or "why" in relation:
            ids.append(link.target_id)
    return list(dict.fromkeys(ids))


def _entity_anchors(record: RecordEnvelope) -> list[str]:
    anchors: list[str] = []
    meta = business_metadata(record.meta)
    containers = [record.content if isinstance(record.content, dict) else {}, record.provenance, meta]
    for container in containers:
        for key in ("project", "project_id", "service", "system", "file", "path", "repo", "agent_id", "workspace_id"):
            value = container.get(key)
            if value:
                anchors.append(str(value).strip().lower())
    anchors.extend(str(tag).strip().lower() for tag in record.tags if str(tag).strip())
    text = _record_text(record)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}|[0-9]{3,}", text):
        lower = token.strip(".,;:()[]{}").lower()
        if not lower or lower in _STOP_TERMS:
            continue
        if any(marker in lower for marker in (".", "/", "-", "_", "eimemory", "openclaw", "rpc", "systemd", "github", "commit", "8091")) or any(ch.isdigit() for ch in lower):
            anchors.append(lower)
    return list(dict.fromkeys(item for item in anchors if item))


def _record_text(record: RecordEnvelope) -> str:
    content = record.content if isinstance(record.content, dict) else {}
    text = "\n".join(
        str(value or "")
        for value in (
            record.title,
            record.summary,
            record.detail,
            content.get("text"),
            content.get("body"),
            content.get("summary"),
        )
        if str(value or "").strip()
    )
    return text[:2400]


def _terms(text: str) -> list[str]:
    terms = []
    for term in re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", str(text or "").lower(), flags=re.UNICODE):
        clean = term.strip(".,;:()[]{}")
        if len(clean) < 2 or clean in _STOP_TERMS:
            continue
        terms.append(clean)
    return list(dict.fromkeys(terms))


def _dedupe_edges(edges: list[MemoryEdge]) -> list[MemoryEdge]:
    by_id: dict[str, MemoryEdge] = {}
    for edge in edges:
        incumbent = by_id.get(edge.edge_id)
        if incumbent is None or edge.confidence > incumbent.confidence:
            by_id[edge.edge_id] = edge
    return sorted(by_id.values(), key=lambda item: (item.edge_type, item.from_id, item.to_id))


def _load_graph_cursor(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    for record in runtime.store.list_records(kinds=["source_watch"], scope=scope, limit=200):
        meta = business_metadata(record.meta)
        if str(meta.get("report_type") or "") == "memory_graph_cursor":
            content = record.content if isinstance(record.content, dict) else {}
            return {
                "record_id": record.record_id,
                "last_seen": str(content.get("last_seen") or ""),
                "seen_record_ids": [str(item) for item in list(content.get("seen_record_ids") or []) if str(item)],
            }
    return {"record_id": "", "last_seen": "", "seen_record_ids": []}


def _save_graph_cursor(runtime: Any, *, scope: ScopeRef, records: list[RecordEnvelope]) -> None:
    high = _high_watermark(records)
    seen_ids = [record.record_id for record in records if _record_time(record) == high]
    existing = _load_graph_cursor(runtime, scope=scope)
    record_id = str(existing.get("record_id") or "")
    if record_id:
        record = runtime.store.get_by_id(record_id, scope=scope)
        if record is not None:
            record.summary = f"Memory graph cursor at {high}"
            record.content = {"last_seen": high, "seen_record_ids": seen_ids}
            record.meta = {**dict(record.meta or {}), "report_type": "memory_graph_cursor"}
            record.touch()
            runtime.store.rewrite(record)
            return
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_watch",
            title=_GRAPH_CURSOR_TITLE,
            summary=f"Memory graph cursor at {high}",
            scope=scope,
            source="eimemory.memory_graph",
            content={"last_seen": high, "seen_record_ids": seen_ids},
            meta={"report_type": "memory_graph_cursor", "semantic_key": stable_semantic_key("memory_graph_cursor", scope.agent_id, scope.workspace_id, scope.user_id)},
        )
    )


def _record_after_cursor(record: RecordEnvelope, cursor: dict[str, Any]) -> bool:
    last_seen = str(cursor.get("last_seen") or "")
    if not last_seen:
        return True
    updated_at = _record_time(record)
    if updated_at > last_seen:
        return True
    if updated_at == last_seen and record.record_id not in set(cursor.get("seen_record_ids") or []):
        return True
    return False


def _is_graph_record_candidate(record: RecordEnvelope) -> bool:
    meta = business_metadata(record.meta)
    report_type = str(meta.get("report_type") or record.provenance.get("report_type") or "").strip()
    if report_type in {"supervisor_run", "memory_graph_cursor"}:
        return False
    if str(record.source or "").startswith("eimemory.memory_graph"):
        return False
    return True


def _high_watermark(records: list[RecordEnvelope]) -> str:
    return max((_record_time(record) for record in records), default="")


def _record_time(record: RecordEnvelope) -> str:
    return str(record.time.updated_at or record.time.occurred_at or record.time.created_at or "")


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _has_entity_like_query(text: str) -> bool:
    if _has_any(text, ("项目", "服务", "系统", "文件", "仓库", "repo", "service", "system", "project", "file", "eimemory", "openclaw", "rpc", "systemd", "8091")):
        return True
    return bool(re.search(r"\b[A-Za-z][A-Za-z0-9_.:/-]{2,}\b|[0-9]{4,}", text))


def _first_value(containers: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value:
                return str(value)
    return ""
