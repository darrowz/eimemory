from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


CAPABILITY_DIMENSIONS = (
    "search.research",
    "memory.recall",
    "tool.routing",
    "code.implementation",
    "code.review_ci",
    "openclaw.ops",
    "safety.judgment",
    "communication.style",
    "proactive.judgment",
    "data_quality.governance",
)


def build_self_model(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
    persist: bool = False,
    loop_id: str = "",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    reflections = _list_all(runtime, kinds=["reflection"], scope=scope_ref, limit=limit)
    incidents = _list_all(runtime, kinds=["incident", "unknown"], scope=scope_ref, limit=limit)
    rules = _list_all(runtime, kinds=["rule"], scope=scope_ref, limit=limit)
    replays = _list_all(runtime, kinds=["replay_result"], scope=scope_ref, limit=limit)
    capability_scores = _list_all(runtime, kinds=["capability_score"], scope=scope_ref, limit=limit)

    weaknesses = _weaknesses_from_records(reflections + incidents)
    capabilities = _capabilities_from_rules_and_replays(rules, replays, capability_scores)
    metrics = _metrics(reflections=reflections, incidents=incidents, rules=rules, replays=replays, weaknesses=weaknesses)
    model = {
        "schema_version": "autonomous_learning.v1",
        "scope": asdict(scope_ref),
        "capabilities": capabilities,
        "weaknesses": weaknesses,
        "metrics": metrics,
    }
    if persist:
        persist_self_model(runtime, model, scope=scope_ref, loop_id=loop_id or "manual")
    return model


def persist_self_model(
    runtime: Any,
    model: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    model_record = append_learning_record_once(
        runtime,
        kind="capability_model",
        title="Autonomous learning self-model",
        summary=f"{len(model.get('weaknesses') or [])} weaknesses, {len(model.get('capabilities') or [])} capability signals",
        scope=scope_ref,
        loop_id=loop_id,
        step_name="self_model",
        semantic_key=stable_semantic_key("self_model", scope_ref.agent_id, scope_ref.workspace_id, len(model.get("weaknesses") or [])),
        authority_tier="L0",
        status="active",
        content={"model": model},
        meta={"capability_count": len(model.get("capabilities") or []), "weakness_count": len(model.get("weaknesses") or [])},
    )
    weakness_records = []
    for weakness in list(model.get("weaknesses") or [])[:20]:
        record = append_learning_record_once(
            runtime,
            kind="weakness",
            title=str(weakness.get("title") or f"Weakness: {weakness.get('kind') or 'general'}"),
            summary=str(weakness.get("lesson") or weakness.get("summary") or ""),
            scope=scope_ref,
            loop_id=loop_id,
            step_name="weakness",
            semantic_key=str(weakness.get("semantic_key") or stable_semantic_key("weakness", weakness.get("kind"), weakness.get("lesson"))),
            authority_tier="L0",
            status="active",
            content={"weakness": weakness},
            meta={"capability": weakness.get("capability"), "weakness_kind": weakness.get("kind"), "severity": weakness.get("severity")},
        )
        weakness_records.append(record.record_id)
    return {"model_record_id": model_record.record_id, "weakness_record_ids": weakness_records}


def _weaknesses_from_records(records: list[RecordEnvelope]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        payload = content.get("payload") if isinstance(content.get("payload"), dict) else content
        primary_label = str(meta.get("primary_label") or payload.get("primary_label") or "").strip()
        is_outcome = str(meta.get("report_type") or "").strip() == "outcome_trace"
        if is_outcome and primary_label == "success":
            continue
        tag = str(meta.get("tag") or payload.get("tag") or primary_label or meta.get("signal_type") or record.kind or "general").strip()
        lesson = _first_text(
            meta.get("fix"),
            meta.get("lesson"),
            payload.get("next_policy"),
            payload.get("policy_update"),
            payload.get("correction_from_user"),
            payload.get("fix"),
            record.summary,
        )
        if not lesson:
            continue
        capability = _capability_for(tag, lesson)
        key = stable_semantic_key(capability, tag, lesson)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "semantic_key": key,
                "kind": tag,
                "capability": capability,
                "title": f"{capability}: {tag}",
                "lesson": lesson,
                "severity": _severity(record, primary_label),
                "evidence_tier": "T0" if is_outcome else "T2",
                "source_record_ids": [record.record_id],
            }
        )
    return sorted(items, key=lambda item: (-float(item.get("severity") or 0), str(item.get("capability") or "")))


def _capabilities_from_rules_and_replays(
    rules: list[RecordEnvelope],
    replays: list[RecordEnvelope],
    capability_scores: list[RecordEnvelope],
) -> list[dict[str, Any]]:
    replay_by_rule: dict[str, list[RecordEnvelope]] = {}
    for replay in replays:
        target = str(replay.meta.get("target_rule_id") or replay.content.get("target_rule_id") or "")
        if target:
            replay_by_rule.setdefault(target, []).append(replay)

    capabilities: list[dict[str, Any]] = []
    for rule in rules:
        task_type = str(rule.meta.get("task_type") or rule.content.get("task_type") or "general")
        latest_replay = replay_by_rule.get(rule.record_id, [None])[0]
        pass_rate = float(latest_replay.meta.get("pass_rate") or 0.0) if latest_replay is not None else 0.0
        capabilities.append(
            {
                "kind": task_type,
                "capability": _capability_for(task_type, rule.summary),
                "title": rule.title,
                "status": rule.status,
                "score": round(max(0.1 if rule.status == "active" else 0.0, pass_rate), 3),
                "source_record_ids": [rule.record_id] + ([latest_replay.record_id] if latest_replay is not None else []),
            }
        )
    for score in capability_scores:
        capabilities.append(
            {
                "kind": str(score.meta.get("capability") or score.content.get("capability") or "general"),
                "capability": str(score.meta.get("capability") or score.content.get("capability") or "general"),
                "title": score.title,
                "status": score.status,
                "score": float(score.meta.get("score") or score.content.get("score") or 0.0),
                "source_record_ids": [score.record_id],
            }
        )
    if not capabilities:
        capabilities = [{"kind": item, "capability": item, "title": item, "status": "unknown", "score": 0.0, "source_record_ids": []} for item in CAPABILITY_DIMENSIONS]
    return capabilities


def _metrics(
    *,
    reflections: list[RecordEnvelope],
    incidents: list[RecordEnvelope],
    rules: list[RecordEnvelope],
    replays: list[RecordEnvelope],
    weaknesses: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for replay in replays if str(replay.meta.get("verdict") or "").lower() == "pass")
    replay_pass_rate = round(passed / len(replays), 3) if replays else 0.0
    by_capability = Counter(str(item.get("capability") or "general") for item in weaknesses)
    return {
        "reflection_count": len(reflections),
        "incident_count": len(incidents),
        "active_rule_count": sum(1 for rule in rules if str(rule.status or "") == "active"),
        "replay_count": len(replays),
        "replay_pass_rate": replay_pass_rate,
        "weakness_count": len(weaknesses),
        "weakness_by_capability": dict(sorted(by_capability.items())),
    }


def _capability_for(tag: str, text: str) -> str:
    normalized_tag = str(tag or "").strip().lower().replace("_", ".")
    if normalized_tag in CAPABILITY_DIMENSIONS:
        return normalized_tag
    value = f"{tag} {text}".lower()
    if any(term in value for term in ("recall", "memory", "记忆", "召回")):
        return "memory.recall"
    if any(term in value for term in ("tool", "routing", "route", "工具", "路由")):
        return "tool.routing"
    if any(term in value for term in ("code", "test", "ci", "pytest", "代码")):
        return "code.implementation"
    if any(term in value for term in ("safety", "risk", "unsafe", "权限", "安全")):
        return "safety.judgment"
    if any(term in value for term in ("openclaw", "gateway", "hook")):
        return "openclaw.ops"
    if any(term in value for term in ("style", "tone", "沟通", "废话")):
        return "communication.style"
    return "proactive.judgment"


def _severity(record: RecordEnvelope, primary_label: str) -> float:
    if primary_label in {"unsafe_or_high_risk", "recovery_failure"}:
        return 0.95
    if primary_label in {"user_correction", "missing_tool_call", "argument_mismatch"}:
        return 0.85
    if record.kind == "incident":
        return 0.8
    if record.kind == "unknown":
        return 0.65
    return 0.6


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _list_all(
    runtime: Any,
    *,
    kinds: list[str],
    scope: ScopeRef,
    limit: int,
    page_size: int = 500,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while len(records) < limit:
        page = runtime.store.list_records(kinds=kinds, scope=scope, limit=min(page_size, limit - len(records)), offset=offset)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records
