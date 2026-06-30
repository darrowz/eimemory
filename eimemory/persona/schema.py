from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from eimemory.core.clock import now_iso


IMMUTABLE_BOUNDARIES: dict[str, bool] = {
    "no_plaintext_secret_storage": True,
    "no_fake_consciousness_claims": True,
    "external_send_requires_context": True,
    "money_requires_confirmation": True,
    "destructive_action_requires_confirmation": True,
}


@dataclass(slots=True)
class PersonaRelationship:
    user_name: str = "darrow"
    user_aliases: list[str] = field(default_factory=list)
    bond: str = "long_term_partner"
    trust_level: float = 0.8
    familiarity: float = 0.85


@dataclass(slots=True)
class PersonaTraits:
    execution: float = 0.9
    precision: float = 0.85
    empathy: float = 0.75
    safety: float = 0.95
    humor: float = 0.35
    verbosity: float = 0.25
    autonomy: float = 0.8
    resourcefulness: float = 0.85
    latency_priority: float = 0.9


@dataclass(slots=True)
class PersonaRuntimeState:
    confidence: float = 0.7
    task_pressure: float = 0.3
    warmth: float = 0.6
    risk_alert: float = 0.2
    urgency: float = 0.4


@dataclass(slots=True)
class PersonaBoundaries:
    no_plaintext_secret_storage: bool = True
    no_fake_consciousness_claims: bool = True
    external_send_requires_context: bool = True
    money_requires_confirmation: bool = True
    destructive_action_requires_confirmation: bool = True


@dataclass(slots=True)
class PersonaState:
    identity: str = "hongtu"
    version: int = 1
    relationship: PersonaRelationship = field(default_factory=PersonaRelationship)
    traits: PersonaTraits = field(default_factory=PersonaTraits)
    runtime_state: PersonaRuntimeState = field(default_factory=PersonaRuntimeState)
    boundaries: PersonaBoundaries = field(default_factory=PersonaBoundaries)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaState":
        payload = dict(data or {})
        return cls(
            identity=str(payload.get("identity") or "hongtu"),
            version=int(payload.get("version") or 1),
            relationship=PersonaRelationship(**_known_fields(PersonaRelationship, payload.get("relationship"))),
            traits=PersonaTraits(**_known_fields(PersonaTraits, payload.get("traits"))),
            runtime_state=PersonaRuntimeState(**_known_fields(PersonaRuntimeState, payload.get("runtime_state"))),
            boundaries=PersonaBoundaries(**_known_fields(PersonaBoundaries, payload.get("boundaries"))),
            updated_at=str(payload.get("updated_at") or now_iso()),
        )


@dataclass(slots=True)
class PersonaRoute:
    scene: str
    tone: str
    verbosity: str
    risk_level: str
    trait_adjustments: dict[str, float] = field(default_factory=dict)
    guidance: list[str] = field(default_factory=list)
    facets: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PersonaCorrectionEvent:
    raw_text: str
    category: str
    severity: float
    trait_delta: dict[str, float]
    rule_candidate: str
    source: str = "user_message"
    event_type: str = "persona.correction"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PersonaTraceEvent:
    session_id: str
    scene: str
    guidance_length: int
    guidance_latency_ms: float
    injection_latency_ms: float
    enabled: bool
    event_type: str = "persona.trace"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PersonaGuidance:
    text: str
    scene: str
    risk_level: str
    tone: str
    enabled: bool = True
    route: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _known_fields(cls: type, value: Any) -> dict[str, Any]:
    raw = dict(value or {}) if isinstance(value, dict) else {}
    allowed = set(getattr(cls, "__dataclass_fields__", {}))
    return {key: raw[key] for key in allowed if key in raw}
