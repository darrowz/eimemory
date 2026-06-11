from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

from eimemory.governance.candidate_search import generate_candidate_policies, score_proxy_candidates
from eimemory.governance.outcome_replay import build_replay_case_from_outcome
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
    feedback_records = _list_all_records(runtime, kinds=["feedback"], scope=scope_ref)
    incident_records = _list_all_records(runtime, kinds=["incident"], scope=scope_ref)
    memory_records = _list_all_records(runtime, kinds=["memory"], scope=scope_ref)
    reflection_records = _list_all_records(runtime, kinds=["reflection"], scope=scope_ref)
    outcome_traces = [record for record in reflection_records if _is_outcome_trace(record)]
    reflections = [record for record in reflection_records if _is_actual_reflection(record)]
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
    candidate_specs.extend(
        _rule_candidates_from_outcome_traces(
            outcome_traces=outcome_traces,
            rules=rules,
        )
    )
    candidate_specs = _standardize_candidate_audit_meta(candidate_specs)
    existing_preference_memory_ids = _existing_preference_memory_ids(rules)
    existing_source_memories = [
        memory.record_id
        for memory in reversed(memory_records)
        if memory.record_id in existing_preference_memory_ids and _is_autonomous_preference_memory(memory)
    ]

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
            rule = runtime.store.append(rule)
            created_rules.append(rule.record_id)
            spec["_created_rule_id"] = rule.record_id

    outcome_replay_results: list[RecordEnvelope] = []
    outcome_promoted_rules: list[str] = []
    if apply:
        outcome_replay_results, outcome_promoted_rules = _replay_and_promote_outcome_rules(
            runtime,
            candidate_specs=candidate_specs,
        )
        replay_results = [*outcome_replay_results, *replay_results]

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
    promoted_rules = [*outcome_promoted_rules, *promoted_rules]
    rules_after = runtime.store.list_records(kinds=["rule"], scope=scope_ref, limit=500) if apply else rules
    active_rule_count = sum(1 for rule in rules_after if rule.status == "active")
    promotion_count = len(promoted_rules) if apply else len(promotion_candidates)
    steady_state = bool(
        apply
        and not candidate_specs
        and not promotion_candidates
        and active_rule_count > 0
        and existing_source_memories
    )

    return {
        "ok": True,
        "apply": bool(apply),
        "scope": scope_payload,
        "candidate_count": len(candidate_specs),
        "created_rule_count": len(created_rules),
        "promoted_count": promotion_count,
        "accepted_rule_count": sum(1 for rule in rules_after if rule.status == "accepted"),
        "active_rule_count": active_rule_count,
        "replay_count": len(replay_results),
        "outcome_replay_count": len(outcome_replay_results),
        "steady_state": steady_state,
        "no_op_reason": "all_candidate_sources_already_materialized" if steady_state else "",
        "roi_summary": roi_summary,
        "source_counts": _candidate_source_counts(candidate_specs),
        "skipped_source_counts": {
            "memory_preference": len(existing_source_memories) if not candidate_specs else 0,
        },
        "record_ids": {
            "source_feedback": [item.record_id for item in _candidate_feedback(candidate_specs)],
            "source_reflections": [item.record_id for item in _candidate_reflections(candidate_specs)],
            "source_incidents": _candidate_record_ids(candidate_specs, "incident_repair"),
            "source_memories": _candidate_record_ids(candidate_specs, "memory_preference"),
            "source_outcome_traces": _candidate_record_ids_for_sources(
                candidate_specs,
                {"diagnosis_pattern", "operator_gap", "visual_evidence_gap", "world_state_mismatch"},
            ),
            "source_diagnosis_patterns": _candidate_record_ids(candidate_specs, "diagnosis_pattern"),
            "source_operator_gaps": _candidate_record_ids(candidate_specs, "operator_gap"),
            "source_visual_evidence_gaps": _candidate_record_ids(candidate_specs, "visual_evidence_gap"),
            "source_world_state_mismatches": _candidate_record_ids(candidate_specs, "world_state_mismatch"),
            "existing_source_memories": existing_source_memories if not candidate_specs else [],
            "created_rules": created_rules,
            "replay_results": [item.record_id for item in replay_results],
            "outcome_replay_results": [item.record_id for item in outcome_replay_results],
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


def _replay_and_promote_outcome_rules(
    runtime: Any,
    *,
    candidate_specs: list[dict],
) -> tuple[list[RecordEnvelope], list[str]]:
    replay_results: list[RecordEnvelope] = []
    promoted_rules: list[str] = []
    for spec in candidate_specs:
        if str(spec.get("source_type") or "") not in {
            "diagnosis_pattern",
            "operator_gap",
            "visual_evidence_gap",
            "world_state_mismatch",
        }:
            continue
        rule_id = str(spec.get("_created_rule_id") or "")
        if not rule_id:
            continue
        rule = runtime.store.get_by_id(rule_id)
        if rule is None or rule.kind != "rule":
            continue
        dataset = list(spec.get("suggested_replay_dataset") or spec.get("audit_meta", {}).get("suggested_replay_dataset") or [])
        replay = _outcome_candidate_replay_result(rule, dataset=dataset, spec=spec)
        replay = runtime.store.append(replay)
        replay_results.append(replay)
        promotion_gate = dict(spec.get("promotion_gate") or spec.get("audit_meta", {}).get("promotion_gate") or {})
        if (
            bool(promotion_gate.get("allow_auto_promote"))
            and str(replay.meta.get("verdict") or "") == "pass"
            and float(replay.meta.get("pass_rate") or 0.0) >= DEFAULT_MIN_PASS_RATE
        ):
            promoted = runtime.evolution.promote_rule(
                record_id=rule.record_id,
                promoter="rule_evolution_loop",
                note="Outcome-derived replay gate passed",
            )
            promoted_rules.append(promoted.record_id)
    return replay_results, promoted_rules


def _outcome_candidate_replay_result(
    rule: RecordEnvelope,
    *,
    dataset: list[dict],
    spec: dict,
) -> RecordEnvelope:
    evaluation_text = _full_text(
        " ".join(
            [
                rule.title,
                rule.summary,
                str(rule.content.get("task_type") or ""),
                json.dumps(rule.content.get("retrieval_policy") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(rule.content.get("response_policy") or {}, ensure_ascii=False, sort_keys=True),
            ]
        )
    ).lower()
    scores: list[float] = []
    samples: list[dict[str, Any]] = []
    for sample in dataset:
        if not isinstance(sample, dict):
            continue
        expected = _coerce_string_list(sample.get("expect_any_text") or sample.get("expected_text"))
        negative = _coerce_string_list(sample.get("negative_expected_text"))
        expected_pass = True if not expected else any(_clean_text(item).lower() in evaluation_text for item in expected)
        negative_pass = not any(_clean_text(item).lower() in evaluation_text for item in negative)
        passed = bool(expected_pass and negative_pass)
        scores.append(1.0 if passed else 0.0)
        samples.append(
            {
                "source_outcome_trace_id": str(sample.get("source_outcome_trace_id") or ""),
                "primary_label": str(sample.get("primary_label") or ""),
                "signals": _coerce_string_list(sample.get("signals")),
                "expected_pass": expected_pass,
                "negative_pass": negative_pass,
                "passed": passed,
            }
        )
    pass_rate = round(sum(scores) / len(scores), 3) if scores else 0.0
    verdict = "pass" if pass_rate >= DEFAULT_MIN_PASS_RATE else "fail"
    audit_meta = dict(spec.get("audit_meta") or {})
    return RecordEnvelope.create(
        kind="replay_result",
        title=f"Outcome replay for {rule.title}",
        summary=f"Outcome replay verdict: {verdict}",
        scope=rule.scope,
        source="eimemory.rule_evolution_loop",
        meta={
            "target_rule_id": rule.record_id,
            "pass_rate": pass_rate,
            "sample_size": len(scores),
            "verdict": verdict,
            "replay_source": "outcome_trace_suggested_replay",
            "candidate_source": str(spec.get("candidate_source") or audit_meta.get("candidate_source") or ""),
            "source_outcome_trace_ids": _coerce_string_list(audit_meta.get("source_outcome_trace_ids")),
        },
        content={
            "dataset_size": len(dataset),
            "samples": samples,
            "suggested_replay_dataset": dataset,
        },
    )


def _full_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _rule_candidates_from_incidents(
    *,
    incident_records: list[RecordEnvelope],
    rules: list[RecordEnvelope],
) -> list[dict]:
    existing_source_keys = _existing_evolution_source_keys(rules)
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
        repair_hint_source = "explicit" if repair_hint else ""
        if not repair_hint:
            repair_hint = _derive_incident_repair_hint(incident, payload)
            repair_hint_source = "derived" if repair_hint else ""
        source_record_ids = [incident.record_id]
        source_key = _source_key("incident_repair", source_record_ids)
        if not repair_hint or incident.record_id in existing_incident_ids or source_key in existing_source_keys:
            continue

        summary = repair_hint
        task_type = _candidate_task_type_from_incident(incident, payload)
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
            "repair_hint_source": repair_hint_source,
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
    existing_memory_ids = _existing_preference_memory_ids(rules)
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


def _rule_candidates_from_outcome_traces(
    *,
    outcome_traces: list[RecordEnvelope],
    rules: list[RecordEnvelope],
) -> list[dict]:
    existing_source_keys = _existing_evolution_source_keys(rules)
    ordered_outcome_traces = sorted(outcome_traces, key=lambda record: (record.time.created_at, record.record_id))
    replay_cases = [
        replay_case
        for replay_case in (build_replay_case_from_outcome(record) for record in ordered_outcome_traces)
        if replay_case
    ]
    seed_candidates = generate_candidate_policies(replay_cases)
    seed_candidates.extend(
        _single_outcome_trace_seed_candidates(
            outcome_traces=ordered_outcome_traces,
            replay_cases=replay_cases,
        )
    )
    candidates: list[dict] = []
    seen_source_keys: set[str] = set()
    for seed in seed_candidates:
        source_key = str(seed.get("source_key") or seed.get("audit_meta", {}).get("evolution_source_key") or "")
        if not source_key or source_key in existing_source_keys or source_key in seen_source_keys:
            continue
        seen_source_keys.add(source_key)
        scored = score_proxy_candidates([seed], replay_cases)
        ranked = list(scored.get("ranked_candidates") or [])
        candidate = ranked[0] if ranked else dict(seed)
        proxy_eval = dict(candidate.get("proxy_eval") or scored.get("proxy_eval") or {})
        audit_meta = dict(candidate.get("audit_meta") or {})
        audit_meta.update(
            {
                "candidate_source": str(candidate.get("candidate_source") or ""),
                "search_stage": "seed",
                "proxy_eval": proxy_eval,
                "promotion_gate": dict(candidate.get("promotion_gate") or audit_meta.get("promotion_gate") or {}),
                "risk_level": str(candidate.get("risk_level") or audit_meta.get("risk_level") or "medium"),
                "evolution_source": "rule_evolution_loop",
                "evolution_source_type": str(candidate.get("source_type") or candidate.get("candidate_source") or ""),
                "evolution_source_key": source_key,
                "evolution_source_record_ids": _coerce_string_list(candidate.get("source_record_ids")),
                "source_outcome_trace_ids": _coerce_string_list(candidate.get("source_trace_ids")),
                "source_trace_ids": _coerce_string_list(candidate.get("source_trace_ids")),
                "suggested_replay_dataset": list(candidate.get("suggested_replay_dataset") or []),
            }
        )
        candidates.append(
            {
                "title": str(candidate.get("title") or f"Rule: {candidate.get('summary', '')}"),
                "summary": str(candidate.get("summary") or ""),
                "task_type": str(candidate.get("task_type") or "outcome.replay"),
                "retrieval_policy": dict(candidate.get("retrieval_policy") or {}),
                "response_policy": dict(candidate.get("response_policy") or {"summary": str(candidate.get("summary") or "")}),
                "feedback": None,
                "reflection": None,
                "source_type": str(candidate.get("source_type") or candidate.get("candidate_source") or ""),
                "candidate_source": str(candidate.get("candidate_source") or candidate.get("source_type") or ""),
                "source_records": [],
                "source_record_ids": _coerce_string_list(candidate.get("source_record_ids")),
                "source_key": source_key,
                "risk_level": str(candidate.get("risk_level") or "medium"),
                "suggested_replay_dataset": list(candidate.get("suggested_replay_dataset") or []),
                "promotion_gate": dict(candidate.get("promotion_gate") or {}),
                "proxy_eval": proxy_eval,
                "initial_status": str(candidate.get("initial_status") or "shadow"),
                "audit_meta": audit_meta,
            }
        )
    return candidates


def _derive_incident_repair_hint(incident: RecordEnvelope, payload: dict) -> str:
    text = _first_incident_text(
        payload.get("summary"),
        incident.summary,
        payload.get("detail"),
        incident.detail,
        payload.get("reason"),
        payload.get("failure"),
        payload.get("error"),
        payload.get("observed"),
        payload.get("actual"),
        payload.get("title"),
        incident.title,
    )
    return f"Prevent recurrence of: {text}" if text else ""


def _first_incident_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            for key in ("summary", "detail", "message", "reason", "error", "observed", "actual"):
                text = _clean_text(value.get(key))
                if text:
                    return text
            continue
        if isinstance(value, (list, tuple, set)):
            text = _clean_text(" ".join(str(item or "") for item in value))
        else:
            text = _clean_text(value)
        if text:
            return text
    return ""


def _single_outcome_trace_seed_candidates(
    *,
    outcome_traces: list[RecordEnvelope],
    replay_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trace_by_id = {record.record_id: record for record in outcome_traces}
    candidates: list[dict[str, Any]] = []
    for replay_case in replay_cases:
        source_trace_id = str(replay_case.get("source_outcome_trace_id") or "")
        record = trace_by_id.get(source_trace_id)
        if record is None:
            continue
        allowed_sources = _singleton_outcome_candidate_sources(record, replay_case)
        if not allowed_sources:
            continue
        for seed in generate_candidate_policies([replay_case], repeat_threshold=1):
            candidate_source = str(seed.get("candidate_source") or seed.get("source_type") or "")
            if candidate_source not in allowed_sources:
                continue
            audit_meta = dict(seed.get("audit_meta") or {})
            audit_meta["singleton_seed"] = True
            audit_meta["singleton_seed_reason"] = _singleton_seed_reason(record, replay_case, candidate_source)
            seed["audit_meta"] = audit_meta
            candidates.append(seed)
    return candidates


def _singleton_outcome_candidate_sources(record: RecordEnvelope, replay_case: dict[str, Any]) -> set[str]:
    if _outcome_trace_confidence(record) < 0.8 or not _outcome_trace_has_correction(record):
        return set()
    primary_label = _clean_text(replay_case.get("primary_label") or record.meta.get("primary_label")).lower()
    signals = {item.lower() for item in _coerce_nested_string_list(replay_case.get("signals"))}
    if primary_label == "user_correction" or signals.intersection({"operator_correction", "user_correction", "operator_gap"}):
        return {"operator_gap"}
    if primary_label == "state_tracking_error" or signals.intersection({"state_tracking_error", "world_state_mismatch"}):
        return {"world_state_mismatch"}
    if primary_label and primary_label not in {"success", "unknown", "unknown_failure"}:
        return {"diagnosis_pattern"}
    return set()


def _singleton_seed_reason(record: RecordEnvelope, replay_case: dict[str, Any], candidate_source: str) -> str:
    primary_label = _clean_text(replay_case.get("primary_label") or record.meta.get("primary_label"))
    return f"single_high_confidence_{candidate_source}:{primary_label or 'bad_outcome'}"


def _outcome_trace_confidence(record: RecordEnvelope) -> float:
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    diagnosis = content.get("diagnosis") if isinstance(content.get("diagnosis"), dict) else {}
    verifier = payload.get("verifier") if isinstance(payload.get("verifier"), dict) else {}
    for value in (
        record.meta.get("confidence"),
        record.meta.get("diagnosis_confidence"),
        diagnosis.get("confidence"),
        payload.get("confidence"),
        verifier.get("confidence"),
    ):
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
    return 0.0


def _outcome_trace_has_correction(record: RecordEnvelope) -> bool:
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    diagnosis = content.get("diagnosis") if isinstance(content.get("diagnosis"), dict) else {}
    operator_gap = content.get("operator_gap") if isinstance(content.get("operator_gap"), dict) else {}
    world_state = content.get("world_state") if isinstance(content.get("world_state"), dict) else {}
    for source in (diagnosis, payload, operator_gap, world_state):
        for key in ("correction", "fix", "policy_update", "expected_behavior", "required_behavior", "expected"):
            if _clean_text(source.get(key)):
                return True
    return False


def _coerce_nested_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_coerce_nested_string_list(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_coerce_nested_string_list(item))
        return values
    text = _clean_text(value)
    return [text] if text else []


def _existing_evolution_source_keys(rules: list[RecordEnvelope]) -> set[str]:
    return {
        str(rule.meta.get("evolution_source_key") or "")
        for rule in rules
        if str(rule.meta.get("evolution_source_key") or "")
    }


def _list_all_records(
    runtime: Any,
    *,
    kinds: list[str],
    scope: ScopeRef,
    page_size: int = 500,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=kinds, scope=scope, limit=page_size, offset=offset)
        records.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return records


def _standardize_candidate_audit_meta(candidate_specs: list[dict]) -> list[dict]:
    for spec in candidate_specs:
        source_type = str(spec.get("source_type") or "feedback")
        source_record_ids = _coerce_string_list(spec.get("source_record_ids"))
        suggested_replay_dataset = list(spec.get("suggested_replay_dataset") or [])
        proxy_eval = dict(spec.get("proxy_eval") or {})
        promotion_gate = dict(spec.get("promotion_gate") or {})
        if not promotion_gate:
            promotion_gate = {
                "allow_auto_promote": False,
                "requires_replay": True,
                "requires_review": True,
                "blocked_reason": "seed_candidate_requires_replay",
            }
        risk_level = str(spec.get("risk_level") or "low")
        audit_meta = dict(spec.get("audit_meta") or {})
        audit_meta.setdefault("candidate_source", str(spec.get("candidate_source") or source_type))
        audit_meta.setdefault("search_stage", "seed")
        audit_meta.setdefault("proxy_eval", proxy_eval)
        audit_meta.setdefault("promotion_gate", promotion_gate)
        audit_meta.setdefault("risk_level", risk_level)
        audit_meta.setdefault("evolution_source_type", source_type)
        audit_meta.setdefault("evolution_source_record_ids", source_record_ids)
        audit_meta.setdefault("source_trace_ids", source_record_ids)
        audit_meta.setdefault("source_outcome_trace_ids", source_record_ids if source_type in {"diagnosis_pattern", "operator_gap", "visual_evidence_gap", "world_state_mismatch"} else [])
        audit_meta.setdefault("suggested_replay_dataset", suggested_replay_dataset)
        spec["candidate_source"] = str(spec.get("candidate_source") or source_type)
        spec["proxy_eval"] = proxy_eval or dict(audit_meta.get("proxy_eval") or {})
        spec["promotion_gate"] = promotion_gate or dict(audit_meta.get("promotion_gate") or {})
        spec["risk_level"] = risk_level
        spec["suggested_replay_dataset"] = suggested_replay_dataset or list(audit_meta.get("suggested_replay_dataset") or [])
        spec["audit_meta"] = audit_meta
    return candidate_specs


def _existing_preference_memory_ids(rules: list[RecordEnvelope]) -> set[str]:
    return {
        memory_id
        for rule in rules
        if str(rule.meta.get("evolution_source_type") or "") == "memory_preference"
        for memory_id in _coerce_string_list(rule.meta.get("evolution_source_record_ids"))
    }


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
    if (
        record.source == "eimemory.rule_evolution_loop"
        and str(record.meta.get("replay_source") or "") != "outcome_trace_suggested_replay"
    ):
        return False
    if str(record.meta.get("report_type") or "") == "rule_evolution":
        return False
    if isinstance(record.content.get("report"), dict):
        return False
    return True


def _is_actual_reflection(record: RecordEnvelope) -> bool:
    if _is_outcome_trace(record):
        return False
    if record.source in {"eimemory.daily_brief", "eimemory.rule_evolution_loop"}:
        return False
    if str(record.meta.get("report_type") or "") in {"daily_brief", "rule_evolution"}:
        return False
    if isinstance(record.content.get("brief"), dict) or isinstance(record.content.get("report"), dict):
        return False
    return True


def _is_outcome_trace(record: RecordEnvelope) -> bool:
    return (
        record.kind == "reflection"
        and str(record.meta.get("report_type") or "") == "outcome_trace"
        and str(record.meta.get("schema_version") or "") == "outcome_trace.v1"
    )


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


def _candidate_record_ids_for_sources(candidate_specs: list[dict], source_types: set[str]) -> list[str]:
    seen: set[str] = set()
    record_ids: list[str] = []
    for spec in candidate_specs:
        if str(spec.get("source_type") or "") not in source_types:
            continue
        for record_id in _coerce_string_list(spec.get("source_record_ids")):
            if record_id in seen:
                continue
            seen.add(record_id)
            record_ids.append(record_id)
    return record_ids


def _candidate_source_counts(candidate_specs: list[dict]) -> dict[str, int]:
    counts = {
        "feedback": 0,
        "incident_repair": 0,
        "memory_preference": 0,
        "diagnosis_pattern": 0,
        "operator_gap": 0,
        "visual_evidence_gap": 0,
        "world_state_mismatch": 0,
    }
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
        "candidate_source": str(spec.get("candidate_source") or source_type),
        "source_record_ids": source_record_ids,
        "source_key": str(spec.get("source_key") or ""),
        "risk_level": str(spec.get("risk_level") or spec.get("audit_meta", {}).get("risk_level") or ""),
        "proxy_eval": dict(spec.get("proxy_eval") or spec.get("audit_meta", {}).get("proxy_eval") or {}),
        "promotion_gate": dict(spec.get("promotion_gate") or spec.get("audit_meta", {}).get("promotion_gate") or {}),
        "suggested_replay_dataset": list(spec.get("suggested_replay_dataset") or spec.get("audit_meta", {}).get("suggested_replay_dataset") or []),
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
