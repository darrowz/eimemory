from __future__ import annotations

from typing import Any
from collections import OrderedDict

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


THOUGHT_STATUSES = {"candidate", "active", "completed", "blocked"}


def generate_thoughts(
    runtime: Any,
    *,
    signals: list[dict[str, Any]] | None = None,
    self_model: dict[str, Any] | None = None,
    goals: list[dict[str, Any]] | None = None,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
    persist: bool = True,
    max_items: int = 20,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    candidates = _candidate_thoughts(signals or [], self_model or {}, goals or [])
    ranked = rank_thoughts(_merge_repeat_candidates(candidates))[:max_items]
    records: list[RecordEnvelope] = []
    if persist:
        for thought in ranked:
            records.append(_upsert_thought(runtime, thought, scope=scope_ref, loop_id=loop_id))
    return {
        "ok": True,
        "thought_count": len(ranked),
        "persisted_record_ids": [record.record_id for record in records],
        "thoughts": [record_to_thought(record) for record in records] if persist else ranked,
    }


def rank_thoughts(thoughts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [_scored_thought(item) for item in thoughts],
        key=lambda item: (
            -float(item.get("score") or 0.0),
            -float(item.get("importance") or 0.0),
            -float(item.get("urgency") or 0.0),
            str(item.get("question") or ""),
        ),
    )


def promote_thoughts_to_goals(
    thoughts: list[dict[str, Any]] | list[RecordEnvelope],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = []
    for raw in thoughts:
        thought = record_to_thought(raw) if isinstance(raw, RecordEnvelope) else dict(raw)
        if str(thought.get("status") or "candidate") == "blocked":
            continue
        capability = str(thought.get("target_capability") or "proactive.judgment")
        question = str(thought.get("question") or thought.get("hypothesis") or "What should be learned from this thought?")
        goals.append(
            {
                "goal_type": "proactive_thought",
                "title": f"Investigate {capability}: {_short(question, 72)}",
                "question": question,
                "expected_artifact": "rule_sop_eval_or_skill",
                "success_criteria": str(thought.get("next_action") or "Produce a reusable asset and validation case."),
                "authority_tier": str(thought.get("authority_tier") or "L1"),
                "priority": float(thought.get("score") or thought.get("importance") or 0.5),
                "target_capability": capability,
                "thought_id": str(thought.get("record_id") or thought.get("thought_id") or ""),
                "source_goal_id": str(thought.get("source_goal_id") or thought.get("goal_id") or ""),
                "source_type": "thought_queue",
                "source_record_ids": list(thought.get("source_record_ids") or []),
                "evidence_tier": str(thought.get("evidence_tier") or "T2"),
                "semantic_key": stable_semantic_key("thought_goal", thought.get("record_id"), capability, question),
            }
        )
        if len(goals) >= limit:
            break
    return goals


def list_open_thoughts(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = runtime.store.list_records(kinds=["thought"], scope=scope_ref, limit=limit)
    return [record_to_thought(record) for record in records if str(record.status or "") in {"candidate", "active"}]


def record_to_thought(record: RecordEnvelope) -> dict[str, Any]:
    content = dict(record.content or {})
    thought = content.get("thought") if isinstance(content.get("thought"), dict) else {}
    meta = dict(record.meta or {})
    return {
        "record_id": record.record_id,
        "status": str(record.status or ""),
        "question": str(thought.get("question") or record.summary or record.title),
        "hypothesis": str(thought.get("hypothesis") or ""),
        "next_action": str(thought.get("next_action") or ""),
        "target_capability": str(thought.get("target_capability") or meta.get("target_capability") or "proactive.judgment"),
        "importance": float(thought.get("importance") or meta.get("importance") or 0.0),
        "urgency": float(thought.get("urgency") or meta.get("urgency") or 0.0),
        "confidence": float(thought.get("confidence") or meta.get("confidence") or 0.0),
        "score": float(thought.get("score") or meta.get("score") or 0.0),
        "repeat_count": int(thought.get("repeat_count") or meta.get("repeat_count") or 1),
        "source_record_ids": list(thought.get("source_record_ids") or []),
        "evidence_tier": str(thought.get("evidence_tier") or meta.get("evidence_tier") or "T2"),
        "authority_tier": str(meta.get("authority_tier") or "L1"),
        "source_type": str(meta.get("source_type") or thought.get("source_type") or "thought"),
        "thought_type": str(meta.get("thought_type") or thought.get("source_type") or "proactive"),
        "thought_id": str(record.record_id),
        "source_goal_id": str(meta.get("source_goal_id") or thought.get("source_goal_id") or ""),
        "status": str(record.status or "candidate"),
    }


def _candidate_thoughts(signals: list[dict[str, Any]], self_model: dict[str, Any], goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for signal in signals:
        summary = str(signal.get("summary") or signal.get("title") or "").strip()
        if not summary:
            continue
        cap = str(signal.get("target_capability") or "proactive.judgment")
        items.append(
            {
                "question": f"What should change because of this signal: {_short(summary, 140)}?",
                "hypothesis": summary,
                "next_action": "Create a reusable asset or explicit rejection after replay/eval.",
                "target_capability": cap,
                "importance": float(signal.get("impact") or signal.get("score") or 0.55),
                "urgency": float(signal.get("urgency") or 0.45),
                "confidence": float(signal.get("confidence") or 0.5),
                "repeat_count": int(signal.get("repeat_count") or 1),
                "source_record_ids": [str(value) for value in [signal.get("record_id"), *(signal.get("source_record_ids") or [])] if value],
                "evidence_tier": str(signal.get("evidence_tier") or "T2"),
                "source_type": str(signal.get("signal_type") or "world_signal"),
            }
        )
    for weakness in self_model.get("weaknesses") or []:
        cap = str(weakness.get("capability") or "proactive.judgment")
        lesson = str(weakness.get("lesson") or weakness.get("kind") or cap)
        items.append(
            {
                "question": f"How can this repeated weakness be prevented: {_short(lesson, 140)}?",
                "hypothesis": lesson,
                "next_action": f"Build a {cap} policy/SOP/eval case for: {_short(lesson, 96)}.",
                "target_capability": cap,
                "importance": 0.75 + min(0.2, float(weakness.get("severity") or 0.0) / 5),
                "urgency": 0.65,
                "confidence": 0.72,
                "repeat_count": int(weakness.get("repeat_count") or 1),
                "source_record_ids": list(weakness.get("source_record_ids") or []),
                "evidence_tier": str(weakness.get("evidence_tier") or "T1"),
                "source_type": "self_model_weakness",
            }
        )
    for goal in goals:
        cap = str((goal.get("sub_capabilities") or ["proactive.judgment"])[0])
        title = str(goal.get("title") or "long-term goal")
        items.append(
            {
                "question": f"What progress can be made now toward long-term goal: {_short(title, 140)}?",
                "hypothesis": title,
                "next_action": "Select one small measurable learning asset.",
                "target_capability": cap,
                "importance": 0.7,
                "urgency": 0.4,
                "confidence": 0.7,
                "repeat_count": 1,
                "source_record_ids": [],
                "evidence_tier": "T0",
                "source_type": "goal_registry",
                "source_goal_id": str(goal.get("id") or ""),
            }
        )
    return items


def _upsert_thought(runtime: Any, thought: dict[str, Any], *, scope: ScopeRef, loop_id: str) -> RecordEnvelope:
    key = stable_semantic_key("thought", thought.get("target_capability"), thought.get("question"))
    existing = _find_by_semantic_key(runtime, scope=scope, semantic_key=key)
    scored = _scored_thought(thought)
    if existing is not None:
        payload = dict(existing.content or {})
        current = payload.get("thought") if isinstance(payload.get("thought"), dict) else {}
        current["repeat_count"] = int(current.get("repeat_count") or existing.meta.get("repeat_count") or 1) + int(scored.get("repeat_count") or 1)
        current["source_record_ids"] = sorted({*list(current.get("source_record_ids") or []), *list(scored.get("source_record_ids") or [])})
        current["score"] = max(float(current.get("score") or 0.0), float(scored.get("score") or 0.0))
        payload["thought"] = {**current, **{k: v for k, v in scored.items() if k not in {"repeat_count", "source_record_ids"}}}
        existing.content = payload
        existing.meta["repeat_count"] = current["repeat_count"]
        existing.meta["score"] = payload["thought"]["score"]
        existing.meta["source_type"] = str(scored.get("source_type") or scored.get("thought_type") or "thought")
        if scored.get("source_goal_id"):
            existing.meta["source_goal_id"] = str(scored.get("source_goal_id"))
        if scored.get("thought_id"):
            existing.meta["thought_id"] = str(scored.get("thought_id"))
        if str(existing.status or "") not in THOUGHT_STATUSES:
            existing.status = "candidate"
        existing.touch()
        return runtime.store.rewrite(existing)
    return append_learning_record_once(
        runtime,
        kind="thought",
        title=f"Thought: {scored.get('target_capability')}",
        summary=str(scored.get("question") or ""),
        scope=scope,
        loop_id=loop_id,
        step_name="think",
        semantic_key=key,
        authority_tier="L1",
        status="candidate",
        content={"thought": scored},
        meta={
            "thought_type": str(scored.get("source_type") or "proactive"),
            "source_type": str(scored.get("source_type") or "thought"),
            "target_capability": str(scored.get("target_capability") or "proactive.judgment"),
            "importance": scored.get("importance"),
            "urgency": scored.get("urgency"),
            "confidence": scored.get("confidence"),
            "score": scored.get("score"),
            "source_goal_id": str(scored.get("source_goal_id") or ""),
            "thought_id": str(scored.get("thought_id") or ""),
            "repeat_count": scored.get("repeat_count"),
            "evidence_tier": scored.get("evidence_tier"),
            "status": "candidate",
        },
    )


def _find_by_semantic_key(runtime: Any, *, scope: ScopeRef, semantic_key: str) -> RecordEnvelope | None:
    for record in runtime.store.list_records(kinds=["thought"], scope=scope, limit=500):
        if str(record.meta.get("semantic_key") or "") == semantic_key:
            return record
    return None


def _scored_thought(thought: dict[str, Any]) -> dict[str, Any]:
    importance = _clamp(float(thought.get("importance") or 0.0))
    urgency = _clamp(float(thought.get("urgency") or 0.0))
    confidence = _clamp(float(thought.get("confidence") or 0.0))
    repeat = max(1, int(thought.get("repeat_count") or 1))
    score = _clamp(importance * 0.45 + urgency * 0.25 + confidence * 0.2 + min(0.1, (repeat - 1) * 0.03))
    return {**thought, "importance": importance, "urgency": urgency, "confidence": confidence, "repeat_count": repeat, "score": score}


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _short(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _merge_repeat_candidates(thoughts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for thought in thoughts:
        scored = _scored_thought(thought)
        key = stable_semantic_key("thought", scored.get("target_capability"), scored.get("question"))
        if key in grouped:
            current = grouped[key]
            current["repeat_count"] = int(current.get("repeat_count") or 1) + int(scored.get("repeat_count") or 1)
            current["score"] = max(float(current.get("score") or 0.0), float(scored.get("score") or 0.0))
            current["source_record_ids"] = _unique_strings(
                list(current.get("source_record_ids") or []) + list(scored.get("source_record_ids") or [])
            )
            if float(scored.get("importance") or 0.0) > float(current.get("importance") or 0.0):
                current["importance"] = scored.get("importance")
            if float(scored.get("urgency") or 0.0) > float(current.get("urgency") or 0.0):
                current["urgency"] = scored.get("urgency")
            if float(scored.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
                current["confidence"] = scored.get("confidence")
            continue
        grouped[key] = dict(scored)
    return list(grouped.values())


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
