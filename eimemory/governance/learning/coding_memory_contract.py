from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import RecordEnvelope, ScopeRef


CODING_OBSERVATION_REPORT_TYPE = "coding_observation"
CODING_GRAPH_QUERY_REPORT_TYPE = "coding_graph_query"
CODING_GRAPH_REPLAY_REPORT_TYPE = "coding_graph_replay"
CODING_MEMORY_SCHEMA_VERSION = "coding_memory_contract.v1"

RELATION_EDGE_TYPES = {
    "PERFORMED_BY": "entity",
    "IN_PROJECT": "entity",
    "TOUCHED_FILE": "entity",
    "USED_TOOL": "entity",
    "RAN_COMMAND": "temporal",
    "FAILED_WITH": "causal",
    "DECIDED_BECAUSE": "causal",
    "PRODUCED_OUTCOME": "causal",
    "VERIFIED_BY": "semantic",
    "PREVENTED_BY_REPLAY": "causal",
}


def observe_coding_memory(
    runtime: Any,
    observation: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {"ok": False, "error": "invalid_observation"}
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized = _normalize_observation(observation)
    record_id = f"codingobs_{_stable_hash(asdict(scope_ref), _observation_identity(normalized))[:24]}"
    nodes = _typed_nodes(record_id, normalized)
    edges = _typed_edges(record_id=record_id, nodes=nodes, scope=scope_ref, observation=normalized)
    relations = sorted({str(edge.meta.get("relation") or "") for edge in edges if edge.meta.get("relation")})
    summary = _observation_summary(normalized, relations)
    record = RecordEnvelope.create(
        kind="memory",
        title=f"Coding session: {normalized.get('session_id') or normalized.get('task', {}).get('title') or record_id}",
        summary=summary,
        detail=json.dumps(normalized, ensure_ascii=False, sort_keys=True)[:1600],
        scope=scope_ref,
        source="eimemory.coding_memory",
        status="active",
        content={
            "schema_version": CODING_MEMORY_SCHEMA_VERSION,
            "report_type": CODING_OBSERVATION_REPORT_TYPE,
            "memory_type": "coding_session",
            "observation": normalized,
            "graph_nodes": nodes,
            "relations": relations,
        },
        tags=["coding-memory", "graph-first", "memory-contract"],
        evidence=[str(item) for item in normalized.get("evidence", []) if str(item).strip()],
        meta={
            "schema_version": CODING_MEMORY_SCHEMA_VERSION,
            "report_type": CODING_OBSERVATION_REPORT_TYPE,
            "memory_type": "coding_session",
            "session_id": str(normalized.get("session_id") or ""),
            "project": str((normalized.get("project") or {}).get("name") or ""),
            "relation_count": len(relations),
            "node_count": len(nodes),
            "force_capture": True,
        },
    )
    record.record_id = record_id
    existing = runtime.store.get_by_id(record_id, scope=scope_ref)
    if existing is None:
        persisted = runtime.store.append(record)
    else:
        record.time.created_at = existing.time.created_at
        record.touch()
        persisted = runtime.store.rewrite(record)
    runtime.store.upsert_memory_edges(edges)
    return {
        "ok": True,
        "schema_version": CODING_MEMORY_SCHEMA_VERSION,
        "report_type": CODING_OBSERVATION_REPORT_TYPE,
        "record_id": persisted.record_id,
        "scope": asdict(scope_ref),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "relations": relations,
        "nodes": nodes,
    }


def query_coding_memory_graph(
    runtime: Any,
    query: str,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    safe_limit = _safe_limit(limit, default=5, cap=50)
    records = _coding_observation_records(runtime, scope=scope_ref, limit=max(20, safe_limit * 6))
    terms = _query_terms(query)
    ranked = sorted(
        ((record, _match_score(record, terms)) for record in records),
        key=lambda item: (item[1], item[0].time.updated_at, item[0].record_id),
        reverse=True,
    )
    selected = [record for record, score in ranked if score > 0][:safe_limit]
    if not selected and records and not terms:
        selected = records[:safe_limit]
    paths = []
    all_edges = []
    for record in selected:
        edges = runtime.store.list_memory_edges(scope=scope_ref, record_ids=[record.record_id], limit=80)
        all_edges.extend(edges)
        steps = [_edge_step(edge) for edge in edges if str(edge.meta.get("relation") or "")]
        if steps:
            paths.append(
                {
                    "record_id": record.record_id,
                    "title": record.title,
                    "summary": record.summary,
                    "steps": steps,
                }
            )
    return {
        "ok": True,
        "schema_version": CODING_MEMORY_SCHEMA_VERSION,
        "report_type": CODING_GRAPH_QUERY_REPORT_TYPE,
        "query": str(query or ""),
        "scope": asdict(scope_ref),
        "path_count": len(paths),
        "paths": paths,
        "evidence_refs": [_evidence_ref(record, all_edges) for record in selected],
    }


def run_coding_graph_replay(
    runtime: Any,
    *,
    query: str,
    expected_relations: list[str] | None = None,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    graph = query_coding_memory_graph(runtime, query, scope=scope_ref, limit=5)
    expected = [str(item).strip().upper() for item in list(expected_relations or []) if str(item).strip()]
    observed = sorted({str(step.get("relation") or "").upper() for path in graph.get("paths", []) for step in path.get("steps", [])})
    missing = [relation for relation in expected if relation not in observed]
    pass_count = len(expected) - len(missing)
    pass_rate = round(pass_count / len(expected), 3) if expected else (1.0 if graph.get("paths") else 0.0)
    ok = bool(graph.get("paths")) and not missing
    report = {
        "ok": ok,
        "schema_version": CODING_MEMORY_SCHEMA_VERSION,
        "report_type": CODING_GRAPH_REPLAY_REPORT_TYPE,
        "query": str(query or ""),
        "scope": asdict(scope_ref),
        "verdict": "pass" if ok else "fail",
        "expected_relations": expected,
        "observed_relations": observed,
        "missing_relations": missing,
        "pass_rate": pass_rate,
        "graph_path_count": int(graph.get("path_count") or 0),
        "persisted_record_id": "",
    }
    if persist:
        record = RecordEnvelope.create(
            kind="replay_result",
            title=f"Coding graph replay: {query}",
            summary=f"{report['verdict']} pass_rate={pass_rate}",
            scope=scope_ref,
            source="eimemory.coding_memory",
            status="active" if ok else "candidate",
            content={**report, "graph": graph},
            meta={
                "schema_version": CODING_MEMORY_SCHEMA_VERSION,
                "report_type": CODING_GRAPH_REPLAY_REPORT_TYPE,
                "verdict": report["verdict"],
                "pass_rate": pass_rate,
                "expected_relation_count": len(expected),
                "missing_relation_count": len(missing),
            },
            evidence=[ref["record_id"] for ref in graph.get("evidence_refs", []) if ref.get("record_id")],
        )
        persisted = runtime.store.append(record)
        report["persisted_record_id"] = persisted.record_id
    return report


def audit_coding_memory_contract(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    safe_limit = _safe_limit(limit, default=50, cap=200)
    records = _coding_observation_records(runtime, scope=scope_ref, limit=safe_limit)
    replay_records = runtime.store.list_records_by_meta_value(
        kinds=["replay_result"],
        scope=scope_ref,
        meta_key="report_type",
        meta_value=CODING_GRAPH_REPLAY_REPORT_TYPE,
        limit=safe_limit,
    ) or []
    return {
        "ok": True,
        "schema_version": CODING_MEMORY_SCHEMA_VERSION,
        "report_type": "coding_memory_audit",
        "scope": asdict(scope_ref),
        "observation_count": len(records),
        "replay_count": len(replay_records),
        "latest_observation_id": records[0].record_id if records else "",
        "stable_tools": ["memory.observe", "memory.remember", "memory.search", "memory.graph", "memory.replay", "memory.audit"],
    }


def _normalize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _first_text(observation.get("session_id"), observation.get("id")),
        "task": _dict(observation.get("task")),
        "agent": _dict(observation.get("agent")),
        "project": _dict(observation.get("project")),
        "files": _list_of_dicts(observation.get("files")),
        "tools": _list_of_dicts(observation.get("tools")),
        "commands": _list_of_dicts(observation.get("commands")),
        "errors": _list_of_dicts(observation.get("errors")),
        "decisions": _list_of_dicts(observation.get("decisions")),
        "outcomes": _list_of_dicts(observation.get("outcomes")),
        "replay_cases": _list_of_dicts(observation.get("replay_cases")),
        "evidence": [str(item) for item in list(observation.get("evidence") or []) if str(item).strip()],
        "observed_at": _first_text(observation.get("observed_at"), now_iso()),
    }


def _observation_identity(observation: dict[str, Any]) -> dict[str, Any]:
    identity = dict(observation)
    identity.pop("observed_at", None)
    return identity


def _typed_nodes(record_id: str, observation: dict[str, Any]) -> list[dict[str, str]]:
    nodes = [{"id": record_id, "type": "coding_session", "label": _first_text(observation.get("session_id"), record_id)}]
    agent = observation.get("agent") or {}
    if agent:
        nodes.append({"id": f"agent:{_slug(_first_text(agent.get('id'), agent.get('name')))}", "type": "agent", "label": _first_text(agent.get("name"), agent.get("id"))})
    project = observation.get("project") or {}
    if project:
        nodes.append({"id": f"project:{_slug(_first_text(project.get('name'), project.get('repo')))}", "type": "project", "label": _first_text(project.get("name"), project.get("repo"))})
    for item in observation.get("files", []):
        path = _first_text(item.get("path"), item.get("file"))
        if path:
            nodes.append({"id": f"file:{path}", "type": "file", "label": path})
    for item in observation.get("tools", []):
        name = _first_text(item.get("name"), item.get("tool"))
        if name:
            nodes.append({"id": f"tool:{_slug(name)}", "type": "tool", "label": name})
    for item in observation.get("commands", []):
        command = _first_text(item.get("command"), item.get("cmd"))
        if command:
            nodes.append({"id": f"command:{_stable_hash(command)[:16]}", "type": "command", "label": command})
    for item in observation.get("errors", []):
        message = _first_text(item.get("message"), item.get("error"), item.get("type"))
        if message:
            nodes.append({"id": f"error:{_stable_hash(message)[:16]}", "type": "error", "label": message})
    for item in observation.get("decisions", []):
        summary = _first_text(item.get("summary"), item.get("decision"))
        if summary:
            nodes.append({"id": f"decision:{_stable_hash(summary)[:16]}", "type": "decision", "label": summary})
    for item in observation.get("outcomes", []):
        summary = _first_text(item.get("summary"), item.get("status"), item.get("outcome"))
        if summary:
            nodes.append({"id": f"outcome:{_stable_hash(summary)[:16]}", "type": "outcome", "label": summary})
    for item in observation.get("replay_cases", []):
        case_id = _first_text(item.get("case_id"), item.get("id"), item.get("query"))
        if case_id:
            nodes.append({"id": f"replay_case:{_slug(case_id)}", "type": "replay_case", "label": case_id})
    return _dedupe_nodes(nodes)


def _typed_edges(*, record_id: str, nodes: list[dict[str, str]], scope: ScopeRef, observation: dict[str, Any]) -> list[MemoryEdge]:
    node_by_type: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        node_by_type.setdefault(node["type"], []).append(node)
    edges: list[MemoryEdge] = []
    edges.extend(_edges_to_type(record_id, node_by_type, "agent", "PERFORMED_BY", scope, "coding_session_agent"))
    edges.extend(_edges_to_type(record_id, node_by_type, "project", "IN_PROJECT", scope, "coding_session_project"))
    edges.extend(_edges_to_type(record_id, node_by_type, "file", "TOUCHED_FILE", scope, "coding_session_file"))
    edges.extend(_edges_to_type(record_id, node_by_type, "tool", "USED_TOOL", scope, "coding_session_tool"))
    edges.extend(_edges_to_type(record_id, node_by_type, "command", "RAN_COMMAND", scope, "coding_session_command"))
    edges.extend(_edges_to_type(record_id, node_by_type, "error", "FAILED_WITH", scope, "coding_session_error"))
    edges.extend(_edges_to_type(record_id, node_by_type, "decision", "DECIDED_BECAUSE", scope, "coding_session_decision"))
    edges.extend(_edges_to_type(record_id, node_by_type, "outcome", "PRODUCED_OUTCOME", scope, "coding_session_outcome"))
    edges.extend(_edges_to_nodes(record_id, _verification_command_nodes(node_by_type, observation), "command", "VERIFIED_BY", scope, "coding_session_verification"))
    edges.extend(_edges_to_type(record_id, node_by_type, "replay_case", "PREVENTED_BY_REPLAY", scope, "coding_session_replay"))
    return edges


def _edges_to_type(record_id: str, node_by_type: dict[str, list[dict[str, str]]], node_type: str, relation: str, scope: ScopeRef, reason: str) -> list[MemoryEdge]:
    return _edges_to_nodes(record_id, node_by_type.get(node_type, []), node_type, relation, scope, reason)


def _edges_to_nodes(record_id: str, nodes: list[dict[str, str]], node_type: str, relation: str, scope: ScopeRef, reason: str) -> list[MemoryEdge]:
    return [
        MemoryEdge.create(
            from_id=record_id,
            to_id=node["id"],
            edge_type=RELATION_EDGE_TYPES[relation],
            confidence=0.86 if relation in {"FAILED_WITH", "DECIDED_BECAUSE", "VERIFIED_BY"} else 0.76,
            evidence_id=record_id,
            scope=scope,
            reason=reason,
            meta={"relation": relation, "node_type": node_type, "label": node.get("label", "")},
        )
        for node in nodes
    ]


def _verification_command_nodes(node_by_type: dict[str, list[dict[str, str]]], observation: dict[str, Any]) -> list[dict[str, str]]:
    allowed_ids = {
        f"command:{_stable_hash(_first_text(item.get('command'), item.get('cmd')))[:16]}"
        for item in observation.get("commands", [])
        if _is_verification_command(item)
    }
    return [node for node in node_by_type.get("command", []) if node.get("id") in allowed_ids]


def _is_verification_command(command: dict[str, Any]) -> bool:
    if not isinstance(command, dict):
        return False
    status = str(command.get("status") or command.get("outcome") or command.get("result") or "").strip().lower()
    if status in {"failed", "fail", "error", "bad"}:
        return False
    text = " ".join(
        str(value or "")
        for value in (
            command.get("command"),
            command.get("cmd"),
            command.get("tool"),
            command.get("summary"),
            command.get("purpose"),
        )
    ).lower()
    return any(token in text for token in ("pytest", "unittest", "compileall", "verify", "verified", "test ", "tests/", "replay", "health check", "smoke"))


def _coding_observation_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[RecordEnvelope]:
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        records = lookup(
            kinds=["memory"],
            scope=scope,
            meta_key="report_type",
            meta_value=CODING_OBSERVATION_REPORT_TYPE,
            limit=limit,
        )
        if records is not None:
            return list(records)
    return [
        record
        for record in runtime.store.list_records(kinds=["memory"], scope=scope, limit=limit)
        if str(record.meta.get("report_type") or "") == CODING_OBSERVATION_REPORT_TYPE
    ]


def _edge_step(edge: MemoryEdge) -> dict[str, Any]:
    return {
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "edge_type": edge.edge_type,
        "relation": str(edge.meta.get("relation") or ""),
        "node_type": str(edge.meta.get("node_type") or ""),
        "label": str(edge.meta.get("label") or ""),
        "confidence": edge.confidence,
        "evidence_id": edge.evidence_id,
    }


def _evidence_ref(record: RecordEnvelope, edges: list[MemoryEdge]) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "kind": record.kind,
        "title": record.title,
        "source": record.source,
        "updated_at": record.time.updated_at,
        "edge_ids": [edge.edge_id for edge in edges if edge.from_id == record.record_id or edge.to_id == record.record_id],
    }


def _match_score(record: RecordEnvelope, terms: set[str]) -> int:
    if not terms:
        return 1
    content = record.content if isinstance(record.content, dict) else {}
    text = " ".join(
        str(value or "")
        for value in [record.title, record.summary, record.detail, json.dumps(content, ensure_ascii=False, default=str)]
    ).lower()
    return sum(1 for term in terms if term in text)


def _query_terms(query: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", str(query or "").lower())
        if len(term) >= 2
    }


def _observation_summary(observation: dict[str, Any], relations: list[str]) -> str:
    task = observation.get("task") if isinstance(observation.get("task"), dict) else {}
    return (
        f"Graph-first coding observation for {_first_text(task.get('title'), observation.get('session_id'), 'coding session')}; "
        f"relations={', '.join(relations)}"
    )


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _safe_limit(value: Any, *, default: int, cap: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, cap))


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _slug(value: str) -> str:
    text = str(value or "").strip().lower().replace("\\", "/")
    text = re.sub(r"\s+", "-", text)
    return text or _stable_hash(value)[:12]


def _stable_hash(*values: Any) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _dedupe_nodes(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id: dict[str, dict[str, str]] = {}
    for node in nodes:
        if node.get("id"):
            by_id.setdefault(node["id"], node)
    return list(by_id.values())
