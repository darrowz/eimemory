from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.core.record_ids import validate_record_id
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
    record_ids = {"lesson": "", "replay": "", "rule": ""}
    persistence = {
        "status": "not_requested" if not persist else "not_started",
        "atomic": False,
        "failed_stage": "",
        "reason": "",
        "stages": {stage: "not_attempted" for stage in ("lesson", "replay", "rule", "edges")},
        "attempted_stages": [],
        "may_have_additional_writes": False,
    }
    if persist:
        store = getattr(runtime, "store", None)
        if not all(callable(getattr(store, method, None)) for method in ("append", "get_by_exact_ref", "upsert_memory_edges")):
            persistence.update(failed_stage="preflight", reason="correction_store_contract_unavailable")
            return _correction_result(scope_ref, payload, replay_case, record_ids, persistence)
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
        if not _correction_append(store, lesson, "lesson", record_ids, persistence):
            return _correction_result(scope_ref, payload, replay_case, record_ids, persistence)
        replay = RecordEnvelope.create(
            kind="replay_result",
            title=f"Correction replay: {payload['target_capability']}",
            summary="not_run: correction lesson produced a replay case; behavior compliance must be checked after a future answer.",
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "user_correction_replay",
                "verdict": "not_run",
                "case": replay_case,
                "pass_rate": 0.0,
                "verification_status": "pending_post_answer",
                "lesson_record_id": record_ids["lesson"],
            },
            meta={
                "report_type": "user_correction_replay",
                "verdict": "not_run",
                "pass_rate": 0.0,
                "verification_status": "pending_post_answer",
                "target_capability": payload["target_capability"],
            },
            evidence=[record_ids["lesson"]],
        )
        if not _correction_append(store, replay, "replay", record_ids, persistence):
            return _correction_result(scope_ref, payload, replay_case, record_ids, persistence)
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
                "lesson_record_id": record_ids["lesson"],
                "replay_record_id": record_ids["replay"],
            },
            meta={
                "report_type": "ground_truth_behavior_rule",
                "priority": "T0",
                "must_use": True,
                "target_capability": payload["target_capability"],
                "lesson_record_id": record_ids["lesson"],
                "replay_record_id": record_ids["replay"],
            },
            evidence=[record_ids["lesson"], record_ids["replay"]],
            tags=["ground-truth", "behavior-rule", payload["target_capability"]],
        )
        if not _correction_append(store, rule, "rule", record_ids, persistence):
            return _correction_result(scope_ref, payload, replay_case, record_ids, persistence)
        _correction_write_edges(
            store,
            _lesson_edges(record_ids["lesson"], record_ids["replay"], record_ids["rule"], payload, scope=scope_ref),
            persistence,
        )
    return _correction_result(scope_ref, payload, replay_case, record_ids, persistence)


def _correction_result(
    scope: ScopeRef,
    payload: dict[str, str],
    replay_case: dict[str, str],
    record_ids: dict[str, str],
    persistence: dict[str, Any],
) -> dict[str, Any]:
    stages = persistence["stages"]
    preview = persistence["status"] == "not_requested"
    complete = all(stages[stage] == "verified" for stage in ("lesson", "replay", "rule")) and stages["edges"] == "store_commit_receipt"
    if not preview:
        persistence["status"] = (
            "complete" if complete else "partial" if any(record_ids.values())
            else "unknown" if any(value == "unknown" for value in stages.values()) else "not_started"
        )
    replay_report = {
        "ok": preview or stages["replay"] == "verified",
        "report_type": "user_correction_replay",
        "verdict": "not_run",
        "pass_rate": 0.0,
        "verification_status": "pending_post_answer",
    }
    return {
        "ok": preview or complete,
        "report_type": "user_correction_closed_loop",
        "scope": asdict(scope),
        "skipped": False,
        "skipped_reason": "",
        "lesson_record_id": record_ids["lesson"],
        "replay_record_id": record_ids["replay"],
        "ground_truth_rule_id": record_ids["rule"],
        "lesson": payload["lesson"],
        "replay_case": replay_case,
        "replay": replay_report,
        "persistence": deepcopy(persistence),
    }


def _correction_append(
    store: Any,
    proposed: RecordEnvelope,
    stage: str,
    record_ids: dict[str, str],
    persistence: dict[str, Any],
) -> bool:
    """Advance only from an exact-hydrated canonical append receipt.

    Each append owns a separate transaction. An uncertain receipt must not
    cause a retry, compensation, or use of the proposed-but-unverified ID.
    """
    persistence.update(failed_stage=stage, reason="correction_expectation_unavailable")
    try:
        expected = deepcopy(proposed)
        submitted = deepcopy(proposed)
        persistence["stages"][stage] = "unknown"
        persistence["attempted_stages"].append(stage)
        persistence.update(reason="correction_append_failed", may_have_additional_writes=True)
        returned = store.append(submitted)
        persistence["reason"] = "correction_append_receipt_invalid"
        if not _correction_record_matches(returned, expected) or returned.record_id in record_ids.values():
            return False
        persistence["reason"] = "correction_exact_read_failed"
        canonical = store.get_by_exact_ref(
            returned.record_id,
            scope=deepcopy(expected.scope),
            source_id=expected.source_id,
        )
        persistence["reason"] = "correction_canonical_record_unavailable_or_mismatched"
        if not _correction_record_matches(canonical, expected) or canonical.record_id != returned.record_id:
            return False
        record_ids[stage] = canonical.record_id
        persistence["stages"][stage] = "verified"
        persistence.update(failed_stage="", reason="", may_have_additional_writes=False)
        return True
    except Exception:
        # Earlier writes may be durable. Never echo an exception or stored
        # payload, claim rollback, or publish the unverified candidate ID.
        return False


def _correction_json(value: Any) -> str:
    """Type-sensitive JSON comparison; no coercion or non-finite numbers."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _correction_record_matches(record: Any, expected: RecordEnvelope) -> bool:
    try:
        if not isinstance(record, RecordEnvelope) or not isinstance(record.scope, ScopeRef):
            return False
        if not isinstance(record.record_id, str):
            return False
        validate_record_id(record.record_id)
        if (
            asdict(record.scope) != asdict(expected.scope)
            or record.source_id != expected.source_id
            or record.source != expected.source
            or record.kind != expected.kind
            or record.status != "active"
            or _correction_json(record.content) != _correction_json(expected.content)
            or _correction_json(record.evidence) != _correction_json(expected.evidence)
        ):
            return False
        presentation = (record.title, record.summary, record.detail)
        expected_presentation = (expected.title, expected.summary, expected.detail)
        if record.kind == "reflection":
            # Match the store's lesson presentation equivalence. Content above
            # remains exact; timestamps and benign annotations are not identity.
            presentation = "\n".join(str(value or "").strip() for value in presentation if str(value or "").strip()).lower()
            expected_presentation = "\n".join(str(value or "").strip() for value in expected_presentation if str(value or "").strip()).lower()
        if presentation != expected_presentation:
            return False
        if not isinstance(record.meta, dict) or not isinstance(record.provenance, dict):
            return False
        nested = record.meta.get("business_meta", {})
        if not isinstance(nested, dict):
            return False
        semantic_keys = (
            "report_type", "target_capability", "lesson_hash", "lesson_record_id", "replay_record_id",
            "verdict", "pass_rate", "verification_status", "priority", "must_use",
        )
        for key in semantic_keys:
            if key not in expected.meta and key not in expected.content:
                continue
            value = expected.content[key] if key in expected.content else expected.meta[key]
            for declaration in (record.meta, nested):
                if key in declaration and _correction_json(declaration[key]) != _correction_json(value):
                    return False
        if "report_type" in record.provenance and record.provenance["report_type"] != expected.content["report_type"]:
            return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _correction_edge_contract(edge: MemoryEdge) -> dict[str, Any]:
    if not isinstance(edge, MemoryEdge) or not isinstance(edge.scope, ScopeRef):
        raise ValueError("correction_edge_receipt_invalid")
    return {
        "edge_id": edge.edge_id,
        "scope": asdict(edge.scope),
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "edge_type": edge.edge_type,
        "evidence_id": edge.evidence_id,
        "confidence": edge.confidence,
        "reason": edge.reason,
        "meta": edge.meta,
    }


def _correction_write_edges(store: Any, edges: list[MemoryEdge], persistence: dict[str, Any]) -> None:
    persistence.update(failed_stage="edges", reason="correction_edge_expectation_unavailable")
    try:
        expected = {edge.edge_id: _correction_json(_correction_edge_contract(edge)) for edge in edges}
        submitted = deepcopy(edges)
        persistence["stages"]["edges"] = "unknown"
        persistence["attempted_stages"].append("edges")
        persistence.update(reason="correction_edge_write_failed", may_have_additional_writes=True)
        returned = store.upsert_memory_edges(submitted)
        persistence["reason"] = "correction_edge_receipt_invalid"
        if not isinstance(returned, list) or len(returned) != len(expected):
            return
        received = {edge.edge_id: _correction_json(_correction_edge_contract(edge)) for edge in returned}
        if received != expected:
            return
        # RuntimeStore commits before returning. This validates that commit
        # receipt, not a separate graph read or atomicity of all prior appends.
        persistence["stages"]["edges"] = "store_commit_receipt"
        persistence.update(failed_stage="", reason="", may_have_additional_writes=False)
    except Exception:
        # In particular, export flushing can fail after the graph commit.
        return


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
    matched = [rule for rule in rules if _rule_matches(query, rule)] if str(query or "").strip() else rules
    replay_gate = _merged_replay_gate(matched)
    verdict = "matched" if matched else "no_match"
    record_id = ""
    if persist and matched:
        record = RecordEnvelope.create(
            kind="learning_eval",
            title="Ground truth pre-answer gate",
            summary=f"{len(matched)} T0 ground-truth rule(s) matched before answer; answer compliance is pending.",
            scope=scope_ref,
            source="eimemory.correction_replay",
            status="active",
            content={
                "report_type": "ground_truth_pre_answer_gate",
                "query": str(query or ""),
                "verdict": verdict,
                "verification_status": "pending_answer_check",
                "gate_required": bool(matched),
                "matched_rule_count": len(matched),
                "rules": matched,
                "replay_gate": replay_gate,
            },
            meta={
                "report_type": "ground_truth_pre_answer_gate",
                "verdict": verdict,
                "verification_status": "pending_answer_check",
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
        "verdict": verdict,
        "verification_status": "pending_answer_check" if matched else "not_required",
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
        # A correction is valuable even before a Profile/registry target is
        # known.  Do not silently attach it to a compiled catch-all; later
        # attribution can link this durable lesson to a concrete capability.
        "target_capability": _first(correction.get("target_capability"), ""),
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
            reason="correction lesson produced a replay case that still needs future behavior verification",
            meta={"relation": "COVERED_BY_REPLAY_CASE", "node_type": "replay", "label": replay_id},
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
    anchors = tokens + [anchor for token in tokens for anchor in _cjk_anchors(token)]
    return not anchors or any(anchor in haystack for anchor in anchors)


def _split_words(text: str) -> list[str]:
    import re

    return [part for part in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text) if part]


def _cjk_anchors(token: str) -> list[str]:
    import re

    chars = "".join(re.findall(r"[\u4e00-\u9fff]", str(token or "")))
    if len(chars) < 2:
        return []
    anchors = {chars[index : index + 2] for index in range(max(0, len(chars) - 1))}
    anchors.update(chars[index : index + 3] for index in range(max(0, len(chars) - 2)))
    return sorted(anchor for anchor in anchors if len(anchor) >= 2)


def _merged_replay_gate(rules: list[dict[str, Any]]) -> dict[str, Any]:
    if not rules:
        return {
            "expected_behavior": "",
            "gate": "",
            "behavior_check": "",
            "pre_action_protocol": [],
            "verification_status": "not_required",
        }
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
        "verification_status": "pending_answer_check",
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
