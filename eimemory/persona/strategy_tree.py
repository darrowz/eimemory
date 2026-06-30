from __future__ import annotations

from typing import Any

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.schema import PersonaState
from eimemory.persona.state import default_persona_state


def choose_strategy(text: str, *, state: PersonaState | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or default_persona_state()
    route = route_persona_context(text, state=state, recent_context=context)
    include = ["verification"]
    avoid = ["overlong_background", "subjective_inner_life_claims"]
    if route.scene == "coding_plan":
        include.extend(["file_structure", "tests", "deployment_note"])
    if route.scene == "high_risk_security":
        include.extend(["safe_alternative", "confirmation_gate"])
        avoid.append("plaintext_secret_echo")
    return {
        "intent": _intent(text),
        "scene": route.scene,
        "risk_level": route.risk_level,
        "strategy": {
            "tone": route.tone,
            "verbosity": route.verbosity,
            "include": include,
            "avoid": avoid,
        },
    }


def _intent(text: str) -> str:
    lowered = str(text or "").lower()
    if "?" in lowered or "吗" in lowered or "是否" in lowered:
        return "ask_question"
    if any(marker in lowered for marker in ("实现", "修", "部署", "改", "build", "fix")):
        return "execute_task"
    return "respond"
