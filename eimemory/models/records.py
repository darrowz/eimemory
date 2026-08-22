from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id
from eimemory.metadata import normalize_metadata
from eimemory.models.source_partitions import DEFAULT_SOURCE_ID, normalize_source_id
from eimemory.models.identity_aliases import IDENTITY_ALIASES_VERSION, normalize_record_aliases
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
        "knowledge_unit",
        "knowledge_candidate",
        "skill_candidate",
        "news",
        "recall_view",
        "raw_chunk",
        "learning_loop",
        "source_watch",
        "world_signal",
        "thought",
        "initiative",
        "capability_model",
        "capability_audit",
        "weakness",
        "learning_goal",
        "research_task",
        "research_note",
        "learning_experiment",
        "learning_eval",
        "evaluation_packet",
        "evaluator_verdict",
        "stop_judgment",
        "capability_hypothesis",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "rl_transition",
        "rl_policy_value",
        "autonomy_goal_queue",
        "regression_watch",
        "learning_playbook",
        "l5_world_model",
        "l5_strategic_roadmap",
        "l5_self_continuity",
        "l5_assessment",
        "l5_closed_loop",
    }
)

QUALITY_META_KEY = "quality"
RECALL_BUNDLE_COMPACT_SCHEMA = "recall_bundle.compact.v1"
_COMPACT_TEXT_LIMIT = 240
_COMPACT_TITLE_LIMIT = 120
_COMPACT_AUXILIARY_LIMIT = 1


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
    source_id: str = DEFAULT_SOURCE_ID
    aliases: list[str] = field(default_factory=list)
    aliases_version: str = IDENTITY_ALIASES_VERSION

    def __post_init__(self) -> None:
        self._validate_kind(self.kind)
        self.source_id = normalize_source_id(self.source_id)
        self.aliases_version = str(self.aliases_version or IDENTITY_ALIASES_VERSION)
        if self.aliases_version != IDENTITY_ALIASES_VERSION:
            raise ValueError(f"unsupported aliases_version: {self.aliases_version}")
        self.aliases = normalize_record_aliases(self.aliases, kind=self.kind, content=self.content)

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
        source_id: str = DEFAULT_SOURCE_ID,
        aliases: list[str] | tuple[str, ...] | None = None,
        aliases_version: str = IDENTITY_ALIASES_VERSION,
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
            source_id=source_id,
            aliases=normalize_record_aliases(aliases, kind=kind, content=content_payload),
            aliases_version=aliases_version,
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
            source_id=data.get("source_id", DEFAULT_SOURCE_ID),
            aliases=normalize_record_aliases(data.get("aliases"), kind=kind, content=data.get("content") or {}),
            aliases_version=str(data.get("aliases_version") or IDENTITY_ALIASES_VERSION),
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

    def to_compact_dict(
        self,
        *,
        limit: int | None = None,
        include_explanation: bool = False,
        explain: bool | None = None,
    ) -> dict[str, Any]:
        """Return a bounded presentation payload without changing ``to_dict``."""

        if explain is not None:
            include_explanation = bool(explain)
        bounded_limit = max(1, min(50, int(limit))) if limit is not None else min(50, len(self.items))
        payload: dict[str, Any] = {
            "schema_version": RECALL_BUNDLE_COMPACT_SCHEMA,
            "items": [_compact_record(item) for item in self.items[:bounded_limit]],
            "rules": [_compact_record(item) for item in self.rules[:_COMPACT_AUXILIARY_LIMIT]],
            "reflections": [_compact_record(item) for item in self.reflections[:_COMPACT_AUXILIARY_LIMIT]],
            "confidence": round(max(0.0, min(1.0, float(self.confidence))), 3),
            "next_action_hint": _compact_text(self.next_action_hint, maximum=160),
        }
        if include_explanation:
            payload["explanation"] = _compact_explanation(self.explanation)
        return _fit_compact_payload(payload, maximum_bytes=16_384 if bounded_limit > 1 else 4_096)


def _compact_record(record: RecordEnvelope) -> dict[str, Any]:
    meta = record.meta if isinstance(record.meta, dict) else {}
    content = record.content if isinstance(record.content, dict) else {}
    memory_type = str(meta.get("memory_type") or content.get("memory_type") or "").strip()
    payload: dict[str, Any] = {
        "record_id": record.record_id,
        "kind": record.kind,
        "status": record.status,
        "title": _compact_text(record.title, maximum=_COMPACT_TITLE_LIMIT),
        "summary": _compact_text(record.summary or record.detail or content.get("text"), maximum=_COMPACT_TEXT_LIMIT),
        "source": _compact_text(record.source, maximum=96),
        "source_id": _compact_text(record.source_id, maximum=96),
    }
    if memory_type:
        payload["memory_type"] = _compact_text(memory_type, maximum=64)
    return payload


def _compact_explanation(explanation: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "query",
        "scope_strategy",
        "scope_fallback",
        "recall_profile",
        "recall_profile_source",
        "recall_intent",
        "selected_count",
        "retrieval_mode",
        "vector_hits",
        "preference_query",
        "report_query",
        "relevance_selector",
    }
    return {
        key: _compact_value(explanation[key], depth=0)
        for key in sorted(allowed)
        if key in explanation
    }


def _compact_value(value: Any, *, depth: int) -> Any:
    if depth >= 3:
        return _compact_text(value, maximum=160)
    if isinstance(value, dict):
        return {
            str(key): _compact_value(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
            if str(key) not in {"scoring", "living", "fusion", "scope", "query_scopes", "recall_scope_aliases"}
        }
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, depth=depth + 1) for item in list(value)[:16]]
    if isinstance(value, str):
        return _compact_text(value, maximum=320)
    return value


def _compact_text(value: Any, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _fit_compact_payload(payload: dict[str, Any], *, maximum_bytes: int) -> dict[str, Any]:
    """Keep compact output within the caller-facing top-1/top-5 ceilings."""

    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= maximum_bytes:
        return payload
    payload["rules"] = []
    payload["reflections"] = []
    if "explanation" in payload:
        explanation = dict(payload["explanation"])
        explanation.pop("relevance_selector", None)
        payload["explanation"] = explanation
    while len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > maximum_bytes:
        changed = False
        for collection_name in ("items", "rules", "reflections"):
            for item in payload.get(collection_name, []):
                for field_name in ("summary", "title"):
                    value = str(item.get(field_name) or "")
                    if len(value) > 32:
                        item[field_name] = value[: max(32, len(value) // 2)]
                        changed = True
        if not changed:
            break
    return payload
