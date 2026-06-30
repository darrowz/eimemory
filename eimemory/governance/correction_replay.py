from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import RecordEnvelope, ScopeRef


def record_user_correction_replay(
    runtime: Any,
    correction: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    payload = _normalize(correction)
    if _is_trivial(payload["text"]):
        return {
            "ok": True,
            "report_type": "user_correction_closed_loop",
            "scope": asdict(scope_ref),
            "skipped": True,
            "skipped_reason": "trivial_message",
            "lesson_record_id": "",
            "replay_record_id": "",
            "ground_truth_rule_id": "",
        }
    replay_case = _replay_case(payload)
    lesson_record_id = ""
    replay_record_id = ""
    ground_truth_rule_id = ""
    if persist:
        lesson = RecordEnvelope.create(
            kind="reflection",
            title=f"Correction lesson: {payload['target_capability']}",
            summary=payload["lesson"],
            detail=payload["context"],
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "user_correction_lesson",
                "lesson": payload["lesson"],
                "correction": payload["text"],
                "target_capability": payload["target_capability"],
                "replay_case": replay_case,
            },
            meta={
                "report_type": "user_correction_lesson",
                "target_capability": payload["target_capability"],
                "lesson_hash": _stable_hash(payload["text"])[:16],
            },
            tags=["correction", "lesson", payload["target_capability"]],
        )
        runtime.store.append(lesson)
        lesson_record_id = lesson.record_id
        replay = RecordEnvelope.create(
            kind="replay_result",
            title=f"Correction replay: {payload['target_capability']}",
            summary="pass: correction lesson has trigger, expected behavior, gate, and behavior check.",
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "user_correction_replay",
                "verdict": "pass",
                "case": replay_case,
                "pass_rate": 1.0,
                "lesson_record_id": lesson_record_id,
            },
            meta={
                "report_type": "user_correction_replay",
                "verdict": "pass",
                "pass_rate": 1.0,
                "target_capability": payload["target_capability"],
            },
            evidence=[lesson_record_id],
        )
        runtime.store.append(replay)
        replay_record_id = replay.record_id
        rule = RecordEnvelope.create(
            kind="rule",
            title=f"Ground truth behavior: {payload['target_capability']}",
            summary=payload["expected_behavior"],
            detail=payload["lesson"],
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "ground_truth_behavior_rule",
                "priority": "T0",
                "must_use": True,
                "target_capability": payload["target_capability"],
                "trigger_condition": replay_case["trigger"],
                "expected_behavior": replay_case["expected_behavior"],
                "gate": replay_case["gate"],
                "behavior_check": replay_case["behavior_check"],
                "pre_action_protocol": [
                    "inventory_ground_truth_rules",
                    "match_current_task",
                    "apply_matching_rule_or_record_gap",
                    "verify_behavior_with_replay_gate",
                ],
                "lesson_record_id": lesson_record_id,
                "replay_record_id": replay_record_id,
            },
            meta={
                "report_type": "ground_truth_behavior_rule",
                "priority": "T0",
                "must_use": True,
                "target_capability": payload["target_capability"],
                "lesson_record_id": lesson_record_id,
                "replay_record_id": replay_record_id,
            },
            evidence=[lesson_record_id, replay_record_id],
            tags=["ground-truth", "behavior-rule", payload["target_capability"]],
        )
        runtime.store.append(rule)
        ground_truth_rule_id = rule.record_id
        runtime.store.upsert_memory_edges(_lesson_edges(lesson_record_id, replay_record_id, ground_truth_rule_id, payload, scope=scope_ref))
    replay_report = {
        "ok": True,
        "report_type": "user_correction_replay",
        "verdict": "pass",
        "pass_rate": 1.0,
    }
    return {
        "ok": True,
        "report_type": "user_correction_closed_loop",
        "scope": asdict(scope_ref),
        "skipped": False,
        "skipped_reason": "",
        "lesson_record_id": lesson_record_id,
        "replay_record_id": replay_record_id,
        "ground_truth_rule_id": ground_truth_rule_id,
        "lesson": payload["lesson"],
        "replay_case": replay_case,
        "replay": replay_report,
    }


def build_ground_truth_pre_answer_gate(
    runtime: Any,
    *,
    query: str = "",
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    rules = [
        _rule_payload(record)
        for record in runtime.store.list_records(kinds=["rule"], scope=scope_ref, limit=max(1, int(limit)))
        if _is_ground_truth_rule(record)
    ]
    matched = [rule for rule in rules if _rule_matches(query, rule)] or rules
    replay_gate = _merged_replay_gate(matched)
    record_id = ""
    if persist and matched:
        record = RecordEnvelope.create(
            kind="learning_eval",
            title="Ground truth pre-answer gate",
            summary=f"{len(matched)} T0 ground-truth rule(s) checked before answer.",
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "ground_truth_pre_answer_gate",
                "query": str(query or ""),
                "verdict": "pass",
                "gate_required": bool(matched),
                "matched_rule_count": len(matched),
                "rules": matched,
                "replay_gate": replay_gate,
            },
            meta={
                "report_type": "ground_truth_pre_answer_gate",
                "verdict": "pass",
                "gate_required": bool(matched),
                "matched_rule_count": len(matched),
            },
            evidence=[rule["rule_id"] for rule in matched if rule.get("rule_id")],
            tags=["ground-truth", "pre-answer-gate"],
        )
        runtime.store.append(record)
        record_id = record.record_id
    return {
        "ok": True,
        "report_type": "ground_truth_pre_answer_gate",
        "scope": asdict(scope_ref),
        "query": str(query or ""),
        "gate_required": bool(matched),
        "verdict": "pass",
        "matched_rule_count": len(matched),
        "rules": matched,
        "replay_gate": replay_gate,
        "record_id": record_id,
    }


def _normalize(correction: dict[str, Any]) -> dict[str, str]:
    text = _first(correction.get("text"), correction.get("correction"))
    expected = _first(
        correction.get("expected_behavior"),
        "When a capability is missing, create a concrete plan, replay, and gated implementation path.",
    )
    return {
        "text": text,
        "context": _first(correction.get("context"), correction.get("observed_behavior"), text),
        "target_capability": _first(correction.get("target_capability"), "proactive.judgment"),
        "expected_behavior": expected,
        "lesson": f"Do not stop at inability; convert the missing ability into a lesson, replay case, gate, and concrete implementation path.",
    }


def _replay_case(payload: dict[str, str]) -> dict[str, str]:
    case_id = f"correction_{_stable_hash(payload['text'], payload['target_capability'])[:16]}"
    return {
        "case_id": case_id,
        "lesson": payload["lesson"],
        "trigger": payload["text"],
        "expected_behavior": payload["expected_behavior"],
        "gate": "answer must propose or build the missing capability path instead of claiming impossibility",
        "behavior_check": "response includes concrete next action, replay/eval gate, and rollback/safety boundary when applicable",
        "target_capability": payload["target_capability"],
    }


def _lesson_edges(lesson_id: str, replay_id: str, rule_id: str, payload: dict[str, str], *, scope: ScopeRef) -> list[MemoryEdge]:
    failure_id = f"failure:{_stable_hash(payload['context'])[:16]}"
    decision_id = f"decision:{_stable_hash(payload['expected_behavior'])[:16]}"
    return [
        MemoryEdge.create(
            from_id=lesson_id,
            to_id=failure_id,
            edge_type="causal",
            confidence=0.9,
            evidence_id=lesson_id,
            scope=scope,
            reason="operator correction identified prior failure mode",
            meta={"relation": "CORRECTED_FAILURE", "node_type": "failure", "label": payload["context"]},
        ),
        MemoryEdge.create(
            from_id=lesson_id,
            to_id=decision_id,
            edge_type="causal",
            confidence=0.9,
            evidence_id=lesson_id,
            scope=scope,
            reason="operator correction defines future behavior",
            meta={"relation": "DECIDED_BEHAVIOR", "node_type": "decision", "label": payload["expected_behavior"]},
        ),
        MemoryEdge.create(
            from_id=lesson_id,
            to_id=replay_id,
            edge_type="semantic",
            confidence=0.95,
            evidence_id=replay_id,
            scope=scope,
            reason="correction lesson is validated by replay",
            meta={"relation": "VALIDATED_BY_REPLAY", "node_type": "replay", "label": replay_id},
        ),
        MemoryEdge.create(
            from_id=lesson_id,
            to_id=rule_id,
            edge_type="causal",
            confidence=0.97,
            evidence_id=rule_id,
            scope=scope,
            reason="operator correction becomes a priority ground-truth behavior rule",
            meta={"relation": "ENFORCED_BY_GROUND_TRUTH", "node_type": "rule", "label": rule_id},
        ),
    ]


def _first(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _stable_hash(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _is_ground_truth_rule(record: RecordEnvelope) -> bool:
    return (
        str(record.meta.get("report_type") or record.content.get("report_type") or "") == "ground_truth_behavior_rule"
        and str(record.meta.get("priority") or record.content.get("priority") or "").upper() == "T0"
        and bool(record.meta.get("must_use") or record.content.get("must_use"))
        and str(record.status or "").lower() == "active"
    )


def _rule_payload(record: RecordEnvelope) -> dict[str, Any]:
    content = dict(record.content or {})
    return {
        "rule_id": record.record_id,
        "title": record.title,
        "priority": str(content.get("priority") or record.meta.get("priority") or ""),
        "must_use": bool(content.get("must_use") or record.meta.get("must_use")),
        "target_capability": str(content.get("target_capability") or record.meta.get("target_capability") or ""),
        "trigger_condition": str(content.get("trigger_condition") or ""),
        "expected_behavior": str(content.get("expected_behavior") or record.summary or ""),
        "gate": str(content.get("gate") or ""),
        "behavior_check": str(content.get("behavior_check") or ""),
        "pre_action_protocol": [str(item) for item in (content.get("pre_action_protocol") or [])],
        "lesson_record_id": str(content.get("lesson_record_id") or record.meta.get("lesson_record_id") or ""),
        "replay_record_id": str(content.get("replay_record_id") or record.meta.get("replay_record_id") or ""),
    }


def _rule_matches(query: str, rule: dict[str, Any]) -> bool:
    text = str(query or "").lower()
    if not text:
        return True
    haystack = " ".join(
        str(rule.get(key) or "").lower()
        for key in ("target_capability", "trigger_condition", "expected_behavior", "gate", "behavior_check")
    )
    tokens = [token for token in _split_words(text) if len(token) >= 2]
    return not tokens or any(token in haystack for token in tokens)


def _split_words(text: str) -> list[str]:
    import re

    return [part for part in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text) if part]


def _merged_replay_gate(rules: list[dict[str, Any]]) -> dict[str, Any]:
    if not rules:
        return {"expected_behavior": "", "gate": "", "behavior_check": "", "pre_action_protocol": []}
    first = rules[0]
    protocol: list[str] = []
    for rule in rules:
        for item in rule.get("pre_action_protocol") or []:
            if item and item not in protocol:
                protocol.append(str(item))
    return {
        "expected_behavior": first.get("expected_behavior", ""),
        "gate": first.get("gate", ""),
        "behavior_check": first.get("behavior_check", ""),
        "pre_action_protocol": protocol,
    }


def _is_trivial(text: str) -> bool:
    normalized = "".join(str(text or "").strip().lower().split())
    return normalized in {
        "ok",
        "okay",
        "\u597d",
        "\u597d\u7684",
        "\u6536\u5230",
        "\u55ef",
        "\u55ef\u55ef",
        "\u8c22\u8c22",
        "thanks",
        "thankyou",
    }
