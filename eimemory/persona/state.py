from __future__ import annotations

from dataclasses import fields, replace

from eimemory.core.clock import now_iso
from eimemory.persona.schema import IMMUTABLE_BOUNDARIES, PersonaBoundaries, PersonaState


def clamp_trait(value: float, *, floor: float = 0.0) -> float:
    return round(max(floor, min(1.0, float(value))), 3)


def default_persona_state() -> PersonaState:
    return enforce_hard_boundaries(PersonaState())


def enforce_hard_boundaries(state: PersonaState) -> PersonaState:
    for key, expected in IMMUTABLE_BOUNDARIES.items():
        setattr(state.boundaries, key, expected)
    for field in fields(state.traits):
        floor = 0.8 if field.name == "safety" else 0.0
        setattr(state.traits, field.name, clamp_trait(getattr(state.traits, field.name), floor=floor))
    for field in fields(state.runtime_state):
        setattr(state.runtime_state, field.name, clamp_trait(getattr(state.runtime_state, field.name)))
    state.relationship.trust_level = clamp_trait(state.relationship.trust_level)
    state.relationship.familiarity = clamp_trait(state.relationship.familiarity)
    state.traits.safety = clamp_trait(state.traits.safety, floor=0.8)
    state.updated_at = now_iso()
    return state


def apply_trait_delta(state: PersonaState, trait_delta: dict[str, float]) -> PersonaState:
    current = state.to_dict()
    updated = PersonaState.from_dict(current)
    trait_names = {field.name for field in fields(updated.traits)}
    for key, delta in dict(trait_delta or {}).items():
        if key not in trait_names:
            continue
        floor = 0.8 if key == "safety" else 0.0
        setattr(updated.traits, key, clamp_trait(getattr(updated.traits, key) + float(delta), floor=floor))
    return enforce_hard_boundaries(updated)


def disabled_guidance_state() -> PersonaState:
    state = default_persona_state()
    state.runtime_state.confidence = 0.0
    return replace(state, updated_at=now_iso())
