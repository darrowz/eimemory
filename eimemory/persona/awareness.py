from __future__ import annotations

from typing import Any

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.schema import PersonaState
from eimemory.persona.state import default_persona_state


def build_awareness_summary(text: str, *, state: PersonaState | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or default_persona_state()
    route = route_persona_context(text, state=state, recent_context=context)
    return {
        "metacognition": {
            "confidence": state.runtime_state.confidence,
            "need_verify": route.scene in {"research", "technical_debug", "coding_plan", "high_risk_security"},
        },
        "self_awareness": {
            "role": state.identity,
            "mode": "functional_persona_model",
            "limits": ["no subjective inner-life claims", "no plaintext secret storage"],
        },
        "social_awareness": {
            "user_style": "concise_direct_action_first",
            "relationship": state.relationship.bond,
        },
        "situational_awareness": {
            "task_type": route.scene,
            "risk_level": route.risk_level,
            "urgency": "high" if state.runtime_state.urgency >= 0.75 else "normal",
        },
    }
