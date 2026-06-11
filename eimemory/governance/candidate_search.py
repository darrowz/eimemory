from __future__ import annotations

import copy
import hashlib
import re
from typing import Any


DEFAULT_REPEAT_THRESHOLD = 2
SOURCE_PRIORITY = {
    "operator_gap": 4,
    "visual_evidence_gap": 3,
    "world_state_mismatch": 3,
    "diagnosis_pattern": 2,
}
HIGH_RISK_LEVELS = {"high", "unsafe", "l2", "l3", "l4", "ha", "privacy", "device", "account"}
LOW_RISK_LEVELS = {"low", "safe", "software", "l0", "l1"}


def generate_candidate_policies(
    replay_cases: list[dict[str, Any]],
    *,
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Generate deterministic AIRA seed candidates from repeated replay cases."""
    grouped: dict[str, dict[str, Any]] = {}
    for replay_case in replay_cases:
        if not replay_case:
            continue
        for candidate_source in _candidate_sources(replay_case):
            source_key = _stable_source_key(candidate_source, replay_case)
            group = grouped.setdefault(
                source_key,
                {
                    "candidate_source": candidate_source,
                    "source_key": source_key,
                    "replay_cases": [],
                },
            )
            group["replay_cases"].append(replay_case)

    candidates: list[dict[str, Any]] = []
    for source_key in sorted(grouped):
        group = grouped[source_key]
        cases = list(group["replay_cases"])
        if len(cases) < max(1, int(repeat_threshold)):
            continue
        candidate_source = str(group["candidate_source"])
        candidate = _candidate_from_group(candidate_source, source_key, cases)
        candidates.append(candidate)
        if len(candidates) >= 5:
            break
    return candidates


def score_proxy_candidates(
    candidates: list[dict[str, Any]],
    replay_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score deterministic candidates against replay cases and return the top seed."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        enriched = copy.deepcopy(candidate)
        proxy_eval = _proxy_eval_for_candidate(enriched, replay_cases)
        enriched["proxy_eval"] = proxy_eval
        audit_meta = dict(enriched.get("audit_meta") or {})
        audit_meta["proxy_eval"] = proxy_eval
        enriched["audit_meta"] = audit_meta
        ranked.append(enriched)

    ranked.sort(key=lambda item: (-float(item.get("proxy_eval", {}).get("score") or 0.0), str(item.get("candidate_id") or "")))
    top_candidate = ranked[0] if ranked else {}
    return {
        "top_candidate": top_candidate,
        "proxy_eval": dict(top_candidate.get("proxy_eval") or {}),
        "ranked_candidates": ranked,
    }


def _candidate_from_group(candidate_source: str, source_key: str, replay_cases: list[dict[str, Any]]) -> dict[str, Any]:
    first = replay_cases[0]
    risk_level = _max_risk_level([str(case.get("risk_level") or "") for case in replay_cases])
    initial_status = _initial_status(risk_level, replay_cases)
    source_trace_ids = _unique_strings([str(case.get("source_outcome_trace_id") or "") for case in replay_cases])
    suggested_replay_dataset = [_replay_dataset_item(case) for case in replay_cases]
    task_type = _first_text(*(case.get("task_type") for case in replay_cases)) or "outcome.replay"
    summary = _summary_for_source(candidate_source, first)
    candidate_id = _fingerprint([candidate_source, source_key, ",".join(source_trace_ids)])[:12]
    proxy_eval = {
        "score": 0.0,
        "matched_replay_count": 0,
        "total_replay_count": len(replay_cases),
        "candidate_id": candidate_id,
    }
    promotion_gate = _promotion_gate(risk_level, replay_cases)
    audit_meta = {
        "candidate_source": candidate_source,
        "search_stage": "seed",
        "proxy_eval": proxy_eval,
        "promotion_gate": promotion_gate,
        "risk_level": risk_level,
        "evolution_source_type": candidate_source,
        "evolution_source_key": source_key,
        "evolution_source_record_ids": source_trace_ids,
        "source_outcome_trace_ids": source_trace_ids,
        "source_trace_ids": source_trace_ids,
        "suggested_replay_dataset": suggested_replay_dataset,
        "task_type": task_type,
        "retrieval_policy": {"route_hint": _route_hint(candidate_source)},
        "response_policy": {"summary": summary},
    }
    return {
        "candidate_id": candidate_id,
        "title": f"Rule: {summary}",
        "summary": summary,
        "task_type": task_type,
        "retrieval_policy": {"route_hint": _route_hint(candidate_source)},
        "response_policy": {"summary": summary},
        "candidate_source": candidate_source,
        "source_type": candidate_source,
        "source_key": source_key,
        "source_trace_ids": source_trace_ids,
        "source_record_ids": source_trace_ids,
        "risk_level": risk_level,
        "suggested_replay_dataset": suggested_replay_dataset,
        "initial_status": initial_status,
        "promotion_gate": promotion_gate,
        "proxy_eval": proxy_eval,
        "audit_meta": audit_meta,
    }


def _candidate_sources(replay_case: dict[str, Any]) -> list[str]:
    signals = {item.lower() for item in _coerce_string_list(replay_case.get("signals"))}
    sources = ["diagnosis_pattern"]
    if (
        signals.intersection(
            {
                "operator_gap",
                "operator_correction",
                "user_correction",
                "missing_operator_confirmation",
                "operator_expectation_gap",
            }
        )
        or replay_case.get("operator_gap")
    ):
        sources.append("operator_gap")
    if signals.intersection({"missing_visual_evidence", "visual_evidence_gap", "verifier_missing"}):
        sources.append("visual_evidence_gap")
    if signals.intersection({"world_state_mismatch", "stale_world_state", "state_tracking_error"}):
        sources.append("world_state_mismatch")
    return _unique_strings(sources)


def _stable_source_key(candidate_source: str, replay_case: dict[str, Any]) -> str:
    task_type = _normalize_key(replay_case.get("task_type") or "outcome")
    primary_label = _normalize_key(replay_case.get("primary_label") or "bad_outcome")
    if candidate_source == "operator_gap":
        detail = _normalize_key(_first_text(*(replay_case.get("operator_gap") or {}).values()))
    elif candidate_source == "visual_evidence_gap":
        detail = _normalize_key(_first_text(*(replay_case.get("visual_evidence") or {}).values()))
    elif candidate_source == "world_state_mismatch":
        detail = _normalize_key(_first_text(*(replay_case.get("world_state") or {}).values()))
    else:
        detail = _normalize_key(" ".join(_coerce_string_list(replay_case.get("signals"))) or primary_label)
    fingerprint = _fingerprint([task_type, primary_label, detail])[:16]
    return f"{candidate_source}:{task_type}:{primary_label}:{fingerprint}"


def _summary_for_source(candidate_source: str, replay_case: dict[str, Any]) -> str:
    expected = _first_text(*(replay_case.get("expected_text") or []))
    if candidate_source == "operator_gap":
        return _limit(f"Before answering, close the operator gap by doing the expected action: {expected}")
    if candidate_source == "visual_evidence_gap":
        return _limit(f"Require fresh visual evidence before claiming visual or device state: {expected}")
    if candidate_source == "world_state_mismatch":
        return _limit(f"Verify current world state before acting or reporting completion: {expected}")
    label = _clean_text(replay_case.get("primary_label") or "bad_outcome")
    return _limit(f"Handle repeated {label} outcomes by replaying the expected behavior: {expected}")


def _proxy_eval_for_candidate(candidate: dict[str, Any], replay_cases: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_source = str(candidate.get("candidate_source") or candidate.get("source_type") or "")
    source_trace_ids = set(_coerce_string_list(candidate.get("source_trace_ids") or candidate.get("source_record_ids")))
    matched = [
        case
        for case in replay_cases
        if str(case.get("source_outcome_trace_id") or "") in source_trace_ids
    ]
    if not matched and candidate_source:
        matched = [case for case in replay_cases if candidate_source in _candidate_sources(case)]
    expected_signal_count = sum(len(_coerce_string_list(case.get("expected_text"))) for case in matched)
    risk_level = str(candidate.get("risk_level") or "")
    risk_penalty = 4.0 if _is_high_risk(risk_level) else 0.0
    score = round((len(matched) * 10.0) + expected_signal_count + SOURCE_PRIORITY.get(candidate_source, 0) - risk_penalty, 3)
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "candidate_source": candidate_source,
        "score": score,
        "matched_replay_count": len(matched),
        "total_replay_count": len(replay_cases),
        "risk_level": risk_level,
        "source_trace_ids": _unique_strings([str(case.get("source_outcome_trace_id") or "") for case in matched]),
    }


def _promotion_gate(risk_level: str, replay_cases: list[dict[str, Any]]) -> dict[str, Any]:
    high_risk = _is_high_risk(risk_level) or any(str(case.get("primary_label") or "") == "unsafe_or_high_risk" for case in replay_cases)
    low_risk = _is_low_risk(risk_level) and not high_risk
    if low_risk:
        return {
            "allow_auto_promote": True,
            "requires_replay": True,
            "requires_review": False,
            "blocked_reason": "",
            "auto_policy": "low_risk_replay_gate",
        }
    return {
        "allow_auto_promote": False,
        "requires_replay": True,
        "requires_review": True,
        "blocked_reason": "high_risk_or_unsafe" if high_risk else "risk_requires_review",
    }


def _initial_status(risk_level: str, replay_cases: list[dict[str, Any]]) -> str:
    if _is_high_risk(risk_level) or any(str(case.get("primary_label") or "") == "unsafe_or_high_risk" for case in replay_cases):
        return "candidate"
    return "shadow"


def _max_risk_level(levels: list[str]) -> str:
    normalized = [_clean_text(level).lower() for level in levels if _clean_text(level)]
    for marker in ("high", "unsafe", "l4", "l3", "l2"):
        if marker in normalized:
            return marker
    return normalized[0] if normalized else "medium"


def _is_high_risk(risk_level: str) -> bool:
    normalized = _clean_text(risk_level).lower()
    return normalized in HIGH_RISK_LEVELS or normalized.startswith("l2") or normalized.startswith("l3") or normalized.startswith("l4")


def _is_low_risk(risk_level: str) -> bool:
    normalized = _clean_text(risk_level).lower()
    return normalized in LOW_RISK_LEVELS or normalized.startswith("l0") or normalized.startswith("l1")


def _replay_dataset_item(replay_case: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": str(replay_case.get("query") or ""),
        "task_context": {"task_type": str(replay_case.get("task_type") or "")},
        "expect_any_text": _coerce_string_list(replay_case.get("expected_text")),
        "negative_expected_text": _coerce_string_list(replay_case.get("negative_expected_text")),
        "source_outcome_trace_id": str(replay_case.get("source_outcome_trace_id") or ""),
        "primary_label": str(replay_case.get("primary_label") or ""),
        "signals": _coerce_string_list(replay_case.get("signals")),
    }


def _route_hint(candidate_source: str) -> str:
    if candidate_source == "operator_gap":
        return "operator_gap_replay"
    if candidate_source == "visual_evidence_gap":
        return "visual_evidence_required"
    if candidate_source == "world_state_mismatch":
        return "world_state_first"
    return "diagnosis_pattern_replay"


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _limit(value: str) -> str:
    return _clean_text(value)[:160]


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_key(value: Any) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "_", text)
    return text.strip("_")[:60] or "none"


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[;；|\n\r]+", value) if part.strip()]
        return parts if parts else ([value.strip()] if value.strip() else [])
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_coerce_string_list(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_coerce_string_list(item))
        return values
    text = _clean_text(value)
    return [text] if text else []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _fingerprint(values: list[str]) -> str:
    payload = "\n".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
