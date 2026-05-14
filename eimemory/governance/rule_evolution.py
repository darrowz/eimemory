from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


DEFAULT_MIN_PASS_RATE = 0.8


def run_rule_evolution_loop(
    runtime: Any,
    scope: dict,
    apply: bool = False,
    min_roi: float = 0.0,
) -> dict:
    """Run the deterministic feedback -> rule -> replay -> ROI -> promotion loop."""
    scope_ref = ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)
    feedback_records = runtime.store.list_records(kinds=["feedback"], scope=scope_ref, limit=500)
    incident_records = runtime.store.list_records(kinds=["incident"], scope=scope_ref, limit=500)
    memory_records = runtime.store.list_records(kinds=["memory"], scope=scope_ref, limit=500)
    reflections = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope_ref, limit=500)
        if _is_actual_reflection(record)
    ]
    replay_results = [
        record
        for record in runtime.store.list_records(kinds=["replay_result"], scope=scope_ref, limit=500)
        if _is_actual_replay_result(record)
    ]
    rules = runtime.store.list_records(kinds=["rule"], scope=scope_ref, limit=500)
    roi_summary = _build_roi_summary(runtime, scope_payload, replay_results)

    candidate_specs = _rule_candidates_from_feedback(
        feedback_records=feedback_records,
        reflections=reflections,
        rules=rules,
    )
    candidate_specs.extend(
        _rule_candidates_from_incidents(
            incident_records=incident_records,
            rules=rules,
        )
    )
    candidate_specs.extend(
        _rule_candidates_from_preference_memories(
            memory_records=memory_records,
            rules=rules,
        )
    )

    created_rules: list[str] = []
    if apply:
        for spec in candidate_specs:
            rule = runtime.evolution.store_rule(
                title=spec["title"],
                summary=spec["summary"],
                task_type=spec["task_type"],
                retrieval_policy=spec["retrieval_policy"],
                response_policy=spec["response_policy"],
                scope=scope_payload,
                status=str(spec.get("initial_status") or "accepted"),
            )
            rule.meta.update(spec["audit_meta"])
            runtime.store.append(rule)
            created_rules.append(rule.record_id)

    promotion_candidates = _promotion_candidates(
        rules=rules,
        feedback_records=feedback_records,
        replay_results=replay_results,
        min_roi=min_roi,
        roi_summary=roi_summary,
    )
    promoted_rules: list[str] = []
    if apply:
        for candidate in promotion_candidates:
            promoted = runtime.evolution.promote_rule(
                record_id=candidate.record_id,
                promoter="rule_evolution_loop",
                note="Replay pass-rate and ROI threshold met",
            )
            promoted_rules.append(promoted.record_id)
    rules_after = runtime.store.list_records(kinds=["rule"], scope=scope_ref, limit=500) if apply else rules

    return {
        "ok": True,
        "apply": bool(apply),
        "scope": scope_payload,
        "candidate_count": len(candidate_specs),
        "created_rule_count": len(created_rules),
        "promoted_count": len(promoted_rules) if apply else len(promotion_candidates),
        "accepted_rule_count": sum(1 for rule in rules_after if rule.status == "accepted"),
        "active_rule_count": sum(1 for rule in rules_after if rule.status == "active"),
        "replay_count": len(replay_results),
        "roi_summary": roi_summary,
        "source_counts": _candidate_source_counts(candidate_specs),
        "record_ids": {
            "source_feedback": [item.record_id for item in _candidate_feedback(candidate_specs)],
            "source_reflections": [item.record_id for item in _candidate_reflections(candidate_specs)],
            "source_incidents": _candidate_record_ids(candidate_specs, "incident_repair"),
            "source_memories": _candidate_record_ids(candidate_specs, "memory_preference"),
            "created_rules": created_rules,
            "replay_results": [item.record_id for item in replay_results],
            "promotion_candidates": [item.record_id for item in promotion_candidates],
            "promoted_rules": promoted_rules,
        },
        "candidates": [_candidate_report(spec) for spec in candidate_specs],
    }


def _rule_candidates_from_feedback(
    *,
    feedback_records: list[RecordEnvelope],
    reflections: list[RecordEnvelope],
    rules: list[RecordEnvelope],
) -> list[dict]:
    existing_feedback_ids = {
        str(rule.meta.get("evolution_source_feedback_id") or "")
        for rule in rules
        if rule.meta.get("evolution_source_feedback_id")
    }
    latest_reflection = reflections[0] if reflections else None
    candidates: list[dict] = []
    for feedback in reversed(feedback_records):
        if str(feedback.meta.get("decision") or "").lower() != "accept":
            continue
        target_ref = dict(feedback.meta.get("target_ref") or feedback.content.get("target_ref") or {})
        if str(target_ref.get("kind") or "") == "rule":
            continue
        if feedback.record_id in existing_feedback_ids:
            continue
        summary = _candidate_summary(feedback, latest_reflection)
        task_type = _candidate_task_type(feedback, latest_reflection)
        audit_meta = {
            "task_type": task_type,
            "retrieval_policy": {"route_hint": "feedback_rule_candidate"},
            "response_policy": {"summary": summary},
            "evolution_source": "rule_evolution_loop",
            "evolution_source_feedback_id": feedback.record_id,
            "evolution_source_reflection_id": latest_reflection.record_id if latest_reflection else "",
            "target_ref": target_ref,
            "evolution_source_type": "feedback",
        }
        candidates.append(
            {
                "title": f"Rule: {summary}",
                "summary": summary,
                "task_type": task_type,
                "retrieval_policy": {"route_hint": "feedback_rule_candidate"},
                "response_policy": {"summary": summary},
                "feedback": feedback,
                "reflection": latest_reflection,
                "source_type": "feedback",
                "source_records": [feedback],
                "source_record_ids": [feedback.record_id],
                "audit_meta": audit_meta,
            }
        )
    return candidates


def _rule_candidates_from_incidents(
    *,
    incident_records: list[RecordEnvelope],
    rules: list[RecordEnvelope],
) -> list[dict]:
    existing_incident_ids = {
        incident_id
        for rule in rules
        if str(rule.meta.get("evolution_source_type") or "") == "incident_repair"
        for incident_id in _coerce_string_list(rule.meta.get("evolution_source_record_ids"))
    }
    candidates: list[dict] = []
    for incident in reversed(incident_records):
        payload = dict(incident.content.get("payload") or {})
        if not (_coerce_bool(incident.meta.get("eval_failure")) or _coerce_bool(payload.get("eval_failure"))):
            continue
        repair_hint = _clean_text(incident.meta.get("repair_hint") or payload.get("repair_hint") or "")
        if not repair_hint or incident.record_id in existing_incident_ids:
            continue

        summary = repair_hint
        task_type = _candidate_task_type_from_incident(incident, payload)
        source_record_ids = [incident.record_id]
        source_key = _source_key("incident_repair", source_record_ids)
        eval_phase = str(incident.meta.get("eval_phase") or payload.get("eval_phase") or "")
        replay_dataset = payload.get("suggested_replay_dataset")
        suggested_replay_dataset = [dict(item) for item in replay_dataset] if isinstance(replay_dataset, list) else []

        audit_meta = {
            "task_type": task_type,
            "retrieval_policy": {"route_hint": "task_context_first"},
            "response_policy": {"summary": summary},
            "evolution_source": "rule_evolution_loop",
            "evolution_source_type": "incident_repair",
            "evolution_source_key": source_key,
            "evolution_source_record_ids": source_record_ids,
            "incident_record_id": incident.record_id,
            "eval_failure": True,
            "eval_phase": eval_phase,
            "suggested_replay_dataset": suggested_replay_dataset,
        }

        candidates.append(
            {
                "title": f"Rule: {summary}",
                "summary": summary,
                "task_type": task_type,
                "retrieval_policy": {"route_hint": "task_context_first"},
                "response_policy": {"summary": summary},
                "feedback": None,
                "reflection": None,
                "source_type": "incident_repair",
                "source_records": [incident],
                "source_record_ids": source_record_ids,
                "source_key": source_key,
                "suggested_replay_dataset": suggested_replay_dataset,
                "audit_meta": audit_meta,
            }
        )
    return candidates


def _rule_candidates_from_preference_memories(
    *,
    memory_records: list[RecordEnvelope],
    rules: list[RecordEnvelope],
) -> list[dict]:
    existing_memory_ids = {
        memory_id
        for rule in rules
        if str(rule.meta.get("evolution_source_type") or "") == "memory_preference"
        for memory_id in _coerce_string_list(rule.meta.get("evolution_source_record_ids"))
    }
    candidates: list[dict] = []
    for memory in reversed(memory_records):
        if memory.record_id in existing_memory_ids or not _is_autonomous_preference_memory(memory):
            continue
        summary = _clean_text(memory.summary or memory.content.get("text") or memory.detail or memory.title)
        if not summary:
            continue
        source_record_ids = [memory.record_id]
        source_key = _source_key("memory_preference", source_record_ids)
        task_type = _candidate_task_type_from_memory(memory)
        retrieval_policy = {"route_hint": "task_context_first", "recall_profile": "precision"}
        response_policy = {"summary": summary}
        audit_meta = {
            "task_type": task_type,
            "retrieval_policy": retrieval_policy,
            "response_policy": response_policy,
            "evolution_source": "rule_evolution_loop",
            "evolution_source_type": "memory_preference",
            "evolution_source_key": source_key,
            "evolution_source_record_ids": source_record_ids,
            "memory_record_id": memory.record_id,
            "activation_mode": "autonomous",
        }
        candidates.append(
            {
                "title": f"Rule: {summary}",
                "summary": summary,
                "task_type": task_type,
                "retrieval_policy": retrieval_policy,
                "response_policy": response_policy,
                "feedback": None,
                "reflection": None,
                "source_type": "memory_preference",
                "source_records": [memory],
                "source_record_ids": source_record_ids,
                "source_key": source_key,
                "initial_status": "active",
                "audit_meta": audit_meta,
            }
        )
    return candidates


def _is_autonomous_preference_memory(record: RecordEnvelope) -> bool:
    if record.status == "rejected":
        return False
    meta = record.meta if isinstance(record.meta, dict) else {}
    content = record.content if isinstance(record.content, dict) else {}
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    capture_decision = str(quality.get("capture_decision") or meta.get("capture_decision") or "").strip().lower()
    if capture_decision == "reject":
        return False
    memory_type = str(meta.get("memory_type") or content.get("memory_type") or "").strip().lower()
    text = "\n".join(
        str(value or "")
        for value in (record.title, record.summary, record.detail, content.get("text"), content.get("body"))
        if str(value or "").strip()
    )
    explicit_preference = memory_type == "preference" or _looks_like_explicit_preference(text)
    if not explicit_preference:
        return False
    source = str(record.source or "").strip().lower()
    force_capture = bool(meta.get("force_capture") or content.get("force_capture"))
    trusted_sources = {"operator.correction", "openclaw.message_received", "eimemory.user_correction"}
    return source in trusted_sources or force_capture or _coerce_bool(meta.get("evolution_candidate"))


def _candidate_task_type_from_memory(memory: RecordEnvelope) -> str:
    meta = memory.meta if isinstance(memory.meta, dict) else {}
    content = memory.content if isinstance(memory.content, dict) else {}
    for key in ("task_type", "target_task_type"):
        value = str(meta.get(key) or content.get(key) or "").strip()
        if value:
            return value
    text = "\n".join(str(value or "") for value in (memory.title, memory.summary, content.get("text")) if str(value or "").strip())
    if any(marker in text for marker in ("沟通风格", "回复风格", "先给结论", "少解释", "讨厌废话")):
        return "chat.reply"
    return "memory.preference"


def _looks_like_explicit_preference(text: str) -> bool:
    haystack = str(text or "")
    lowered = haystack.lower()
    if "沟通风格" in haystack and any(marker in haystack for marker in ("极简", "直接", "简洁", "废话", "少解释", "先给结论", "结论")):
        return True
    if any(marker in haystack for marker in ("偏好", "喜欢", "讨厌", "不喜欢")) and any(
        marker in haystack for marker in ("极简", "直接", "简洁", "废话", "啰嗦", "长篇", "解释", "结论")
    ):
        return True
    return any(marker in lowered for marker in ("prefer concise", "reply style", "communication style"))


def _candidate_task_type_from_incident(
    incident: RecordEnvelope,
    payload: dict,
) -> str:
    task_type = str(
        payload.get("task_type")
        or payload.get("eval_phase")
        or incident.meta.get("task_type")
        or incident.meta.get("eval_phase")
        or "memory_eval_failure"
    ).strip()
    if task_type:
        return task_type
    return "memory_eval_failure"


def _promotion_candidates(
    *,
    rules: list[RecordEnvelope],
    feedback_records: list[RecordEnvelope],
    replay_results: list[RecordEnvelope],
    min_roi: float,
    roi_summary: dict,
) -> list[RecordEnvelope]:
    if float(roi_summary.get("roi_signal") or 0.0) < float(min_roi):
        return []
    accepted_rules = [rule for rule in rules if rule.status == "accepted"]
    candidates: list[RecordEnvelope] = []
    for rule in accepted_rules:
        feedback = _latest_feedback_for_rule(rule.record_id, feedback_records)
        replay = _latest_replay_for_rule(rule.record_id, replay_results)
        if feedback is None or str(feedback.meta.get("decision") or "") != "accept":
            continue
        if replay is None or str(replay.meta.get("verdict") or "") != "pass":
            continue
        if float(replay.meta.get("pass_rate") or 0.0) < DEFAULT_MIN_PASS_RATE:
            continue
        candidates.append(rule)
    return candidates


def _build_roi_summary(runtime: Any, scope: dict, replay_results: list[RecordEnvelope]) -> dict:
    base = dict(runtime.evolution.build_roi_report(scope=scope))
    replay_count = len(replay_results)
    pass_count = sum(1 for item in replay_results if str(item.meta.get("verdict") or "") == "pass")
    pass_rates = [float(item.meta.get("pass_rate") or 0.0) for item in replay_results]
    base["replay_pass_rate"] = round(pass_count / replay_count, 3) if replay_count else 0.0
    base["average_pass_rate"] = round(sum(pass_rates) / replay_count, 3) if replay_count else 0.0
    return base


def _candidate_summary(feedback: RecordEnvelope, reflection: RecordEnvelope | None) -> str:
    reason = _clean_text(feedback.summary or feedback.content.get("reason") or "")
    if reason:
        return reason
    if reflection is not None:
        fix = _clean_text(reflection.meta.get("fix") or reflection.content.get("fix") or reflection.summary)
        if fix:
            return fix
    return "Capture accepted feedback as a reusable memory rule"


def _candidate_task_type(feedback: RecordEnvelope, reflection: RecordEnvelope | None) -> str:
    if reflection is not None:
        task_type = str(reflection.meta.get("task_type") or reflection.meta.get("tag") or "").strip()
        if task_type:
            return task_type
    target_ref = dict(feedback.meta.get("target_ref") or feedback.content.get("target_ref") or {})
    target_kind = str(target_ref.get("kind") or "memory").strip()
    return target_kind or "memory"


def _latest_feedback_for_rule(rule_id: str, feedback_records: list[RecordEnvelope]) -> RecordEnvelope | None:
    for feedback in feedback_records:
        target_ref = dict(feedback.meta.get("target_ref") or feedback.content.get("target_ref") or {})
        if str(target_ref.get("record_id") or "") == rule_id:
            return feedback
    return None


def _latest_replay_for_rule(rule_id: str, replay_results: list[RecordEnvelope]) -> RecordEnvelope | None:
    for replay in replay_results:
        if str(replay.meta.get("target_rule_id") or "") == rule_id:
            return replay
    return None


def _is_actual_replay_result(record: RecordEnvelope) -> bool:
    if record.source == "eimemory.rule_evolution_loop":
        return False
    if str(record.meta.get("report_type") or "") == "rule_evolution":
        return False
    if isinstance(record.content.get("report"), dict):
        return False
    return True


def _is_actual_reflection(record: RecordEnvelope) -> bool:
    if record.source in {"eimemory.daily_brief", "eimemory.rule_evolution_loop"}:
        return False
    if str(record.meta.get("report_type") or "") in {"daily_brief", "rule_evolution"}:
        return False
    if isinstance(record.content.get("brief"), dict) or isinstance(record.content.get("report"), dict):
        return False
    return True


def _candidate_feedback(candidate_specs: list[dict]) -> list[RecordEnvelope]:
    return [
        spec["feedback"]
        for spec in candidate_specs
        if isinstance(spec.get("source_type"), str)
        and str(spec.get("source_type")) == "feedback"
        and isinstance(spec.get("feedback"), RecordEnvelope)
    ]


def _candidate_reflections(candidate_specs: list[dict]) -> list[RecordEnvelope]:
    seen: set[str] = set()
    records: list[RecordEnvelope] = []
    for spec in candidate_specs:
        reflection = spec.get("reflection")
        if reflection is None or reflection.record_id in seen:
            continue
        seen.add(reflection.record_id)
        records.append(reflection)
    return records


def _candidate_record_ids(candidate_specs: list[dict], source_type: str) -> list[str]:
    seen: set[str] = set()
    record_ids: list[str] = []
    for spec in candidate_specs:
        if str(spec.get("source_type") or "") != source_type:
            continue
        for record_id in _coerce_string_list(spec.get("source_record_ids")):
            if record_id in seen:
                continue
            seen.add(record_id)
            record_ids.append(record_id)
    return record_ids


def _candidate_source_counts(candidate_specs: list[dict]) -> dict[str, int]:
    counts = {"feedback": 0, "incident_repair": 0, "memory_preference": 0}
    for spec in candidate_specs:
        source_type = str(spec.get("source_type") or "feedback")
        counts[source_type] = counts.get(source_type, 0) + 1
    return counts


def _candidate_report(spec: dict) -> dict:
    reflection = spec.get("reflection")
    source_type = str(spec.get("source_type") or "feedback")
    source_record_ids = _coerce_string_list(spec.get("source_record_ids"))
    return {
        "title": spec["title"],
        "summary": spec["summary"],
        "task_type": spec["task_type"],
        "source_feedback_id": spec["feedback"].record_id if source_type == "feedback" and isinstance(spec.get("feedback"), RecordEnvelope) else "",
        "source_reflection_id": reflection.record_id if reflection else "",
        "source_type": source_type,
        "source_record_ids": source_record_ids,
        "initial_status": str(spec.get("initial_status") or "accepted"),
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return False


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, dict):
        if isinstance(value.get("record_id"), str) and value["record_id"]:
            return [str(value["record_id"])]
        return []
    return []


def _source_key(prefix: str, source_record_ids: list[str]) -> str:
    return f"{prefix}:{','.join(source_record_ids)}"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:120]
