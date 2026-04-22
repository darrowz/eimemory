from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id

VALID_KINDS: frozenset[str] = frozenset(
    {
        "memory",
        "source_candidate",
        "incident",
        "reflection",
        "feedback",
        "rule",
        "replay_result",
        "unknown",
        "paper_source",
        "paper_extract",
        "claim_card",
        "entity_record",
        "relation_record",
        "knowledge_page",
        "knowledge_candidate",
        "recall_view",
    }
)

QUALITY_META_KEY = "quality"


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _normalized_terms(text: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[\w]+", text, flags=re.UNICODE) if term.strip()]


def evaluate_memory_quality(
    *,
    text: str,
    title: str = "",
    memory_type: str = "",
    source: str = "",
    force_capture: bool = False,
) -> dict[str, Any]:
    """Return deterministic capture quality metadata for a memory candidate."""
    combined = " ".join(part.strip() for part in (title, text) if part and part.strip())
    terms = _normalized_terms(combined)
    body_terms = _normalized_terms(text)
    normalized = " ".join(terms)
    body_alnum_count = sum(1 for char in text if char.isalnum())
    alnum_count = sum(1 for char in combined if char.isalnum())
    unique_body_terms = len(set(body_terms))
    memory_type = memory_type.lower().strip()
    source = source.lower().strip()

    high_value_keywords = {
        "always",
        "decision",
        "decided",
        "important",
        "must",
        "never",
        "prefer",
        "preference",
        "project",
        "remember",
        "rule",
        "should",
        "\u8bb0\u4f4f",
        "\u504f\u597d",
        "\u51b3\u7b56",
        "\u89c4\u5219",
        "\u9879\u76ee",
        "\u91cd\u8981",
    }
    uncertain_keywords = {"maybe", "perhaps", "guess", "unsure", "\u53ef\u80fd", "\u4e5f\u8bb8", "\u4e0d\u786e\u5b9a"}
    reusable_keywords = {
        "api",
        "architecture",
        "config",
        "contract",
        "deploy",
        "eibrain",
        "eimemory",
        "interface",
        "openclaw",
        "policy",
        "scope",
        "server",
        "tenant",
        "user",
    }
    keyword_hits = sum(1 for keyword in high_value_keywords if keyword in normalized or keyword in combined)
    reusable_hits = sum(1 for keyword in reusable_keywords if keyword in normalized or keyword in combined.lower())
    uncertain_hits = sum(1 for keyword in uncertain_keywords if keyword in normalized or keyword in combined)

    thin_or_noisy = body_alnum_count < 8 or (len(body_terms) <= 2 and body_alnum_count < 20) or unique_body_terms <= 1
    type_bonus = {
        "decision": 0.24,
        "preference": 0.22,
        "rule": 0.2,
        "fact": 0.14,
        "project": 0.14,
        "conversation": 0.02,
    }.get(memory_type, 0.08)
    source_bonus = 0.08 if any(marker in source for marker in ("tool.store", "migration", "cli")) else 0.0

    length_bonus = min(0.18, alnum_count / 420)
    importance = _clamp_score(0.28 + type_bonus + min(0.28, keyword_hits * 0.09) + length_bonus)
    confidence = _clamp_score(0.62 + source_bonus - min(0.24, uncertain_hits * 0.08))
    freshness = 1.0
    reuse_potential = _clamp_score(0.3 + min(0.28, reusable_hits * 0.07) + min(0.18, keyword_hits * 0.045) + type_bonus / 2)
    salience_score = _clamp_score((importance * 0.38) + (confidence * 0.22) + (freshness * 0.12) + (reuse_potential * 0.28))

    capture_decision = "accept"
    if thin_or_noisy and not force_capture:
        capture_decision = "reject"
    elif salience_score < 0.34 and not force_capture:
        capture_decision = "reject"

    if capture_decision == "reject":
        quality_tier = "candidate"
    elif salience_score >= 0.78:
        quality_tier = "core"
    elif salience_score >= 0.55:
        quality_tier = "confirmed"
    else:
        quality_tier = "candidate"

    return {
        "importance": importance,
        "confidence": confidence,
        "freshness": freshness,
        "reuse_potential": reuse_potential,
        "salience_score": salience_score,
        "quality_tier": quality_tier,
        "capture_decision": capture_decision,
    }


@dataclass(slots=True)
class ScopeRef:
    tenant_id: str = "default"
    agent_id: str = ""
    workspace_id: str = ""
    user_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScopeRef":
        data = data or {}
        return cls(
            tenant_id=str(data.get("tenant_id", "default") or "default"),
            agent_id=str(data.get("agent_id", "") or ""),
            workspace_id=str(data.get("workspace_id", "") or ""),
            user_id=str(data.get("user_id", "") or ""),
        )


@dataclass(slots=True)
class TimeRef:
    created_at: str
    updated_at: str
    occurred_at: str

    @classmethod
    def now(cls) -> "TimeRef":
        ts = now_iso()
        return cls(created_at=ts, updated_at=ts, occurred_at=ts)


@dataclass(slots=True)
class LinkRef:
    relation: str
    target_kind: str
    target_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinkRef":
        return cls(
            relation=str(data.get("relation", "")),
            target_kind=str(data.get("target_kind", "")),
            target_id=str(data.get("target_id", "")),
        )


@dataclass(slots=True)
class RecordEnvelope:
    record_id: str
    kind: str
    status: str
    title: str
    summary: str
    detail: str
    content: dict[str, Any]
    tags: list[str]
    links: list[LinkRef]
    evidence: list[str]
    source: str
    scope: ScopeRef
    time: TimeRef
    provenance: dict[str, Any]
    meta: dict[str, Any]

    def __post_init__(self) -> None:
        self._validate_kind(self.kind)

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid record kind: {kind}")

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        title: str,
        scope: ScopeRef,
        summary: str = "",
        detail: str = "",
        content: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        links: list[LinkRef] | None = None,
        evidence: list[str] | None = None,
        source: str = "eimemory",
        status: str = "active",
        provenance: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "RecordEnvelope":
        cls._validate_kind(kind)
        meta_payload = dict(meta or {})
        content_payload = dict(content or {})
        if kind == "memory" and QUALITY_META_KEY not in meta_payload:
            memory_text = str(content_payload.get("text") or summary or detail or title)
            memory_type = str(meta_payload.get("memory_type") or content_payload.get("memory_type") or "")
            force_capture = bool(meta_payload.get("force_capture") or content_payload.get("force_capture"))
            meta_payload[QUALITY_META_KEY] = evaluate_memory_quality(
                text=memory_text,
                title=title,
                memory_type=memory_type,
                source=source,
                force_capture=force_capture,
            )
        return cls(
            record_id=generate_record_id(kind),
            kind=kind,
            status=status,
            title=title,
            summary=summary,
            detail=detail,
            content=content_payload,
            tags=list(tags or []),
            links=list(links or []),
            evidence=list(evidence or []),
            source=source,
            scope=scope,
            time=TimeRef.now(),
            provenance=dict(provenance or {}),
            meta=meta_payload,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordEnvelope":
        kind = str(data["kind"])
        cls._validate_kind(kind)
        time_data = data.get("time") or {}
        if not isinstance(time_data, dict):
            time_data = {}
        default_time = asdict(TimeRef.now())
        time = TimeRef(
            created_at=str(time_data["created_at"])
            if time_data.get("created_at") is not None
            else default_time["created_at"],
            updated_at=str(time_data["updated_at"])
            if time_data.get("updated_at") is not None
            else default_time["updated_at"],
            occurred_at=str(time_data["occurred_at"])
            if time_data.get("occurred_at") is not None
            else default_time["occurred_at"],
        )
        return cls(
            record_id=str(data["record_id"]),
            kind=kind,
            status=str(data.get("status", "active")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            detail=str(data.get("detail", "")),
            content=dict(data.get("content") or {}),
            tags=[str(item) for item in (data.get("tags") or [])],
            links=[LinkRef.from_dict(item) for item in (data.get("links") or [])],
            evidence=[str(item) for item in (data.get("evidence") or [])],
            source=str(data.get("source", "eimemory")),
            scope=ScopeRef.from_dict(data.get("scope")),
            time=time,
            provenance=dict(data.get("provenance") or {}),
            meta=dict(data.get("meta") or {}),
        )

    def touch(self) -> None:
        self.time.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["links"] = [asdict(link) for link in self.links]
        payload["scope"] = asdict(self.scope)
        payload["time"] = asdict(self.time)
        return payload


@dataclass(slots=True)
class RecallBundle:
    items: list[RecordEnvelope]
    rules: list[RecordEnvelope]
    reflections: list[RecordEnvelope]
    confidence: float
    next_action_hint: str
    explanation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "rules": [item.to_dict() for item in self.rules],
            "reflections": [item.to_dict() for item in self.reflections],
            "confidence": self.confidence,
            "next_action_hint": self.next_action_hint,
            "explanation": dict(self.explanation),
        }
