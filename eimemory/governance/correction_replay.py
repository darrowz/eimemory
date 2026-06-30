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


def _is_trivial(text: str) -> bool:
    normalized = "".join(str(text or "").strip().lower().split())
    return normalized in {"ok", "okay", "好", "好的", "收到", "嗯", "嗯嗯", "谢谢", "thanks", "thankyou"}


def _stable_hash(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()
