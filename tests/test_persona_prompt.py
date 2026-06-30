from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.persona.prompt import build_persona_guidance
from eimemory.persona.state import default_persona_state


def test_prompt_guidance_is_short_scene_specific_and_no_fake_consciousness() -> None:
    guidance = build_persona_guidance(
        text="帮我实现 persona layer，补测试并部署",
        state=default_persona_state(),
        max_chars=800,
    )

    assert guidance.text.startswith("Persona guidance:")
    assert len(guidance.text) <= 800
    assert "coding_plan" in guidance.text
    assert "verification" in guidance.text.lower()
    forbidden = ["real consciousness", "sentient", "real emotion", "I feel"]
    assert all(item.lower() not in guidance.text.lower() for item in forbidden)


def test_openclaw_before_prompt_build_includes_persona_guidance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "1")
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    result = hooks.before_prompt_build(
        {
            "session_id": "sess-persona",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "task_context": {"task_type": "coding"},
            "query": "帮我修 bug，补测试并部署",
        }
    )

    assert result["persona_guidance"]["enabled"] is True
    assert "Persona guidance:" in result["persona_guidance"]["text"]
    assert len(result["persona_guidance"]["text"]) <= 800
    assert result["task_context"]["persona_guidance"]["scene"] == "coding_plan"
    assert result["memory_bundle"]["explanation"]["persona_guidance"]["scene"] == "coding_plan"
