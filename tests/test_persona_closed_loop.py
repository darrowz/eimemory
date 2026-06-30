from __future__ import annotations

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.evolver import evolve_persona
from eimemory.persona.store import PersonaStore


def test_persona_correction_evolve_and_openclaw_guidance_closed_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "1")
    runtime = Runtime.create(root=tmp_path)
    store = PersonaStore(runtime.store)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    correction = correction_from_user_text("戏很多啊，别演，直接说结果")
    store.record_correction(correction, scope=scope)

    evolved = evolve_persona(store.load_state(), store.list_corrections(scope=scope), store=store, scope=scope, dry_run=False)
    result = OpenClawMemoryHooks(runtime).before_prompt_build(
        {
            "session_id": "sess-persona-loop",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "query": "回复短一点，直接给结论",
        }
    )

    assert evolved.applied_categories == ["verbosity"]
    assert store.load_state().traits.verbosity < 0.25
    assert result["persona_guidance"]["enabled"] is True
    assert result["persona_guidance"]["route"]["trait_adjustments"]["verbosity"] < 0
    assert "Answer briefly first." in result["persona_guidance"]["text"]


def test_persona_guidance_can_be_disabled_for_openclaw_hook(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "0")
    runtime = Runtime.create(root=tmp_path)

    result = OpenClawMemoryHooks(runtime).before_prompt_build(
        {
            "session_id": "sess-persona-off",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "query": "用 Codex 实现并补测试",
        }
    )

    assert result["persona_guidance"]["enabled"] is False
    assert "persona_guidance" not in result["task_context"]
