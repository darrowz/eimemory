from __future__ import annotations

import os
from typing import Any

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.schema import PersonaGuidance, PersonaState
from eimemory.persona.state import default_persona_state


def persona_enabled() -> bool:
    value = str(os.environ.get("EIMEMORY_PERSONA_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def build_persona_guidance(
    *,
    text: str,
    state: PersonaState | None = None,
    recent_context: dict[str, Any] | None = None,
    max_chars: int = 800,
) -> PersonaGuidance:
    state = state or default_persona_state()
    route = route_persona_context(text, state=state, recent_context=recent_context)
    lines = [
        "Persona guidance:",
        "- Identity: Hongtu, calm professional long-term partner.",
        "- User style: concise, direct, action-first.",
        f"- Current scene: {route.scene}.",
        f"- Tone: {route.tone}; verbosity: {route.verbosity}; risk: {route.risk_level}.",
    ]
    for item in route.guidance:
        line = f"- {item}"
        if line not in lines:
            lines.append(line)
    text_payload = _fit_lines(lines, max_chars=max(120, int(max_chars or 800)))
    return PersonaGuidance(
        text=text_payload,
        scene=route.scene,
        risk_level=route.risk_level,
        tone=route.tone,
        route=route.to_dict(),
    )


def disabled_persona_guidance() -> dict[str, Any]:
    return {"enabled": False, "text": "", "scene": "", "risk_level": "", "tone": ""}


def _fit_lines(lines: list[str], *, max_chars: int) -> str:
    selected: list[str] = []
    for line in lines:
        candidate = "\n".join([*selected, line]).strip()
        if len(candidate) > max_chars:
            break
        selected.append(line)
    if selected:
        return "\n".join(selected)
    return lines[0][:max_chars]
