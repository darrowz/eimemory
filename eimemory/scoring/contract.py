from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "memory_score.v1"


def _clamp(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return round(max(0.0, min(1.0, numeric)), 4)


@dataclass(slots=True)
class ScoreComponent:
    name: str
    value: float
    weight: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.value = _clamp(self.value)
        self.weight = _clamp(self.weight)
        self.evidence = dict(self.evidence or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "weight": self.weight,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreComponent":
        return cls(
            name=str(data.get("name") or ""),
            value=float(data.get("value") or 0.0),
            weight=float(data.get("weight") or 0.0),
            evidence=dict(data.get("evidence") or {}),
        )


@dataclass(slots=True)
class ScoreProvenance:
    entity_id: str
    activity: str
    agent: str
    source: str
    generated_at: str
    inputs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "activity": self.activity,
            "agent": self.agent,
            "source": self.source,
            "generated_at": self.generated_at,
            "inputs": [dict(item) for item in self.inputs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreProvenance":
        return cls(
            entity_id=str(data.get("entity_id") or ""),
            activity=str(data.get("activity") or ""),
            agent=str(data.get("agent") or ""),
            source=str(data.get("source") or ""),
            generated_at=str(data.get("generated_at") or ""),
            inputs=[dict(item) for item in (data.get("inputs") or []) if isinstance(item, dict)],
        )


@dataclass(slots=True)
class ScoreContext:
    activity: str = "record.create"
    profile: str = "balanced"
    source: str = ""
    entity_id: str = ""
    query: str = ""
    force_capture: bool = False
    inputs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MemoryScore:
    final_score: float
    tier: str
    components: dict[str, ScoreComponent]
    labels: list[str]
    explanation: dict[str, Any]
    provenance: ScoreProvenance
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.final_score = _clamp(self.final_score)
        self.components = dict(self.components or {})
        self.labels = [str(label) for label in self.labels if str(label)]
        self.explanation = dict(self.explanation or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "final_score": self.final_score,
            "tier": self.tier,
            "components": {name: component.to_dict() for name, component in self.components.items()},
            "labels": list(self.labels),
            "explanation": dict(self.explanation),
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryScore":
        component_payload = dict(data.get("components") or {})
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            final_score=float(data.get("final_score") or 0.0),
            tier=str(data.get("tier") or "candidate"),
            components={
                name: ScoreComponent.from_dict(value)
                for name, value in component_payload.items()
                if isinstance(value, dict)
            },
            labels=[str(label) for label in (data.get("labels") or [])],
            explanation=dict(data.get("explanation") or {}),
            provenance=ScoreProvenance.from_dict(dict(data.get("provenance") or {})),
        )
