from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


RESEARCH_CLOSURE_REPORT_TYPE = "research_closure_review"
DEFAULT_REVIEW_MODEL = "gpt-5.5"

_POLICY_REPLAY_TERMS = (
    "policy_replay",
    "replay_count",
    "replay buffer",
    "replay buffers",
    "dual replay",
    "continual learning",
    "streaming",
    "single-pass",
    "single pass",
    "eviction",
)

_GROUNDING_TERMS = (
    "grounding",
    "geometry",
    "geometric",
    "vlm",
    "self_model",
    "uncertainty",
    "semantic",
)


def build_research_closure_review(
    runtime: Any,
    candidate_record_or_dict: RecordEnvelope | dict[str, Any],
    promotion_report: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    persist: bool = True,
    review_model: str = DEFAULT_REVIEW_MODEL,
) -> dict[str, Any]:
    """Create the research-to-action closure artifact for promoted paper intake."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    candidate = _candidate_payload(candidate_record_or_dict)
    title = _candidate_text(candidate, "title") or "Promoted research paper"
    text = _combined_text(candidate)
    decision = _closure_decision(text)
    artifact = {
        "report_type": RESEARCH_CLOSURE_REPORT_TYPE,
        "review_model_requested": str(review_model or DEFAULT_REVIEW_MODEL),
        "review_status": "pending_model_review",
        "source_candidate_id": _candidate_id(candidate_record_or_dict, candidate),
        "source_title": title,
        "paper_source_id": str(promotion_report.get("paper_source_id") or ""),
        "promotion_record_ids": list(promotion_report.get("record_ids") or []),
        "decision": decision["decision"],
        "target_capability": decision["target_capability"],
        "landing_point": decision["landing_point"],
        "evidence": decision["evidence"],
        "risk": decision["risk"],
        "next_action": decision["next_action"],
        "scope": asdict(scope_ref),
    }
    record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="replay_result",
            title=f"Research closure review: {title}",
            summary=decision["summary"],
            scope=scope_ref,
            loop_id="research_intake_closure",
            step_name="research_closure_review",
            semantic_key=stable_semantic_key(
                RESEARCH_CLOSURE_REPORT_TYPE,
                artifact["source_candidate_id"],
                artifact["paper_source_id"],
                title,
                decision["landing_point"],
            ),
            authority_tier="L0",
            status="active",
            content=artifact,
            meta={
                "report_type": RESEARCH_CLOSURE_REPORT_TYPE,
                "review_model_requested": artifact["review_model_requested"],
                "review_status": artifact["review_status"],
                "decision": artifact["decision"],
                "target_capability": artifact["target_capability"],
                "landing_point": artifact["landing_point"],
                "source_candidate_id": artifact["source_candidate_id"],
                "paper_source_id": artifact["paper_source_id"],
            },
            source="eimemory.research_closure",
        )
        record_id = record.record_id
    return {
        "ok": True,
        "persisted": bool(persist),
        "record_id": record_id,
        **artifact,
    }


def _closure_decision(text: str) -> dict[str, Any]:
    lowered = text.lower()
    replay_hits = [term for term in _POLICY_REPLAY_TERMS if term in lowered]
    if replay_hits:
        return {
            "decision": "enter_closure",
            "target_capability": "proactive.judgment",
            "landing_point": "policy_replay",
            "summary": "Research maps directly to policy replay closure and should become a replay/candidate follow-up.",
            "evidence": replay_hits,
            "risk": "Keep artifact separate from real task replay metrics until a deterministic case is added.",
            "next_action": "Create or update a policy_replay case that proves research-derived insights produce replay evidence.",
        }
    grounding_hits = [term for term in _GROUNDING_TERMS if term in lowered]
    if grounding_hits:
        return {
            "decision": "observe_only",
            "target_capability": "research.synthesis",
            "landing_point": "self_model_observe",
            "summary": "Research is useful for future hierarchical observe work but is not a direct replay-count repair.",
            "evidence": grounding_hits,
            "risk": "Robotics grounding may overfit embodied-control assumptions before eimemory has matching signals.",
            "next_action": "Keep as self-model uncertainty evidence; revisit when observe planning needs semantic/geometric layers.",
        }
    return {
        "decision": "observe_only",
        "target_capability": "knowledge.intake",
        "landing_point": "research_memory",
        "summary": "Research was promoted to paper memory but has no immediate closed-loop implementation target.",
        "evidence": [],
        "risk": "Avoid creating speculative candidates without an implementation surface.",
        "next_action": "Retain as research memory and wait for a concrete capability gap.",
    }


def _candidate_payload(candidate_record_or_dict: RecordEnvelope | dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate_record_or_dict, RecordEnvelope):
        payload = dict(candidate_record_or_dict.content or {})
        payload.setdefault("record_id", candidate_record_or_dict.record_id)
        payload.setdefault("title", candidate_record_or_dict.title)
        payload.setdefault("summary", candidate_record_or_dict.summary)
        payload.setdefault("content_excerpt", candidate_record_or_dict.detail)
        payload.setdefault("meta", dict(candidate_record_or_dict.meta or {}))
        payload.setdefault("provenance", dict(candidate_record_or_dict.provenance or {}))
        return payload
    return dict(candidate_record_or_dict or {})


def _candidate_id(candidate_record_or_dict: RecordEnvelope | dict[str, Any], candidate: dict[str, Any]) -> str:
    if isinstance(candidate_record_or_dict, RecordEnvelope):
        return candidate_record_or_dict.record_id
    provenance = candidate.get("provenance")
    if isinstance(provenance, dict):
        value = provenance.get("record_id") or provenance.get("source_id")
        if value:
            return str(value)
    return str(candidate.get("record_id") or candidate.get("source_id") or candidate.get("fingerprint") or "")


def _combined_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "summary", "abstract", "body", "text", "content_excerpt"):
        parts.append(_candidate_text(candidate, key))
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        for key in ("original_abstract", "translated_abstract", "summary", "notes"):
            parts.append(str(metadata.get(key) or ""))
    return "\n".join(part for part in parts if part.strip())


def _candidate_text(candidate: dict[str, Any], key: str) -> str:
    value = candidate.get(key)
    if value is None:
        return ""
    return str(value).strip()
