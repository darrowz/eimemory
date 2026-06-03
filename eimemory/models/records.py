from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id
from eimemory.metadata import normalize_metadata
from eimemory.scoring import ScoreContext, evaluate_memory_score, memory_score_to_legacy_quality, with_score_metadata

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
        "news",
        "recall_view",
        "raw_chunk",
        "learning_loop",
        "source_watch",
        "world_signal",
        "thought",
        "initiative",
        "capability_model",
        "weakness",
        "learning_goal",
        "research_task",
        "research_note",
        "learning_experiment",
        "learning_eval",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "regression_watch",
        "learning_playbook",
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
    score = evaluate_memory_score(
        text=text,
        title=title,
        memory_type=memory_type,
        source=source,
        force_capture=force_capture,
        context=ScoreContext(activity="record.create", source="record.create", force_capture=force_capture),
    )
    return memory_score_to_legacy_quality(score)


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
        meta_payload = normalize_metadata(meta or {})
        content_payload = dict(content or {})
        if kind == "memory":
            memory_text = str(content_payload.get("text") or summary or detail or title)
            memory_type = str(meta_payload.get("memory_type") or content_payload.get("memory_type") or "")
            force_capture = bool(meta_payload.get("force_capture") or content_payload.get("force_capture"))
            legacy_quality = meta_payload.get(QUALITY_META_KEY) if isinstance(meta_payload.get(QUALITY_META_KEY), dict) else None
            score = evaluate_memory_score(
                text=memory_text,
                title=title,
                memory_type=memory_type,
                source=source,
                force_capture=force_capture,
                context=ScoreContext(activity="record.create", source="record.create", force_capture=force_capture),
                legacy_quality=legacy_quality,
            )
            meta_payload = with_score_metadata(meta_payload, score, preserve_quality=True)
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
