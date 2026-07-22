from __future__ import annotations

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
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
    assert result["persona_trace"]["stored"] is None
    assert result["persona_trace"]["skipped_reason"] == "persona_disabled"
    assert "persona_guidance" not in result["task_context"]


def test_openclaw_before_prompt_build_records_persona_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "1")
    runtime = Runtime.create(root=tmp_path)

    result = OpenClawMemoryHooks(runtime).before_prompt_build(
        {
            "session_id": "sess-persona-trace",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "帮我修复 persona trace 并给验证结果",
        }
    )

    traces = [
        record
        for record in runtime.store.list_records(
            kinds=["reflection"],
            scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
            limit=10,
        )
        if record.source == "persona.trace"
    ]
    assert traces
    trace = traces[0].content
    assert trace["event_type"] == "persona.trace"
    assert trace["enabled"] is True
    assert trace["scene"] == result["persona_guidance"]["scene"]
    assert trace["guidance_length"] == len(result["persona_guidance"]["text"])
    assert trace["injection_latency_ms"] >= trace["guidance_latency_ms"] >= 0.0


def test_openclaw_before_prompt_build_is_idempotent_for_persona_trace_and_prompt_audit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "1")
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    event = {
        "session_id": "sess-persona-idempotent",
        "agent_id": "main",
        "workspace_id": "repo-x",
        "user_id": "darrow",
        "query": "repeat before prompt build should not duplicate closed-loop evidence",
    }

    first = hooks.before_prompt_build(event)
    second = hooks.before_prompt_build(event)

    scope = ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"})
    traces = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=20)
        if record.source == "persona.trace" and record.scope == scope
    ]
    audits = [
        record
        for record in runtime.store.list_records(kinds=["recall_view"], scope=scope, limit=20)
        if record.source == "openclaw.before_prompt_build" and record.scope == scope
    ]
    assert len(traces) == 1
    assert len(audits) == 1
    assert first["persona_trace"]["stored"]["record_id"] == second["persona_trace"]["stored"]["record_id"]
    assert audits[0].content["task_context"]["fields_filtered"] is True
    assert audits[0].content["persona_guidance"]["text_sha256"]

    stable_content = dict(audits[0].content)
    stable_id = hooks._prompt_audit_record_id(scope=scope, content=stable_content)
    volatile_content = {
        **stable_content,
        "task_context_sha256": "different-runtime-context-digest",
        "persona_guidance_sha256": "different-runtime-guidance-digest",
    }
    assert hooks._prompt_audit_record_id(scope=scope, content=volatile_content) == stable_id
    semantic_context = {
        **stable_content,
        "task_context": {**stable_content["task_context"], "goal": "different business goal"},
    }
    assert hooks._prompt_audit_record_id(scope=scope, content=semantic_context) != stable_id
    semantic_persona = {
        **stable_content,
        "persona_guidance": {
            **stable_content["persona_guidance"],
            "text_sha256": "different-persona-text-digest",
        },
    }
    assert hooks._prompt_audit_record_id(scope=scope, content=semantic_persona) != stable_id


def test_openclaw_before_prompt_build_does_not_record_disabled_persona_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "0")
    runtime = Runtime.create(root=tmp_path)

    result = OpenClawMemoryHooks(runtime).before_prompt_build(
        {
            "session_id": "sess-persona-trace-off",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "persona off smoke",
        }
    )

    traces = [
        record
        for record in runtime.store.list_records(
            kinds=["reflection"],
            scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
            limit=10,
        )
        if record.source == "persona.trace"
    ]
    assert result["persona_guidance"]["enabled"] is False
    assert result["persona_trace"]["stored"] is None
    assert result["persona_trace"]["skipped_reason"] == "persona_disabled"
    assert traces == []


def test_openclaw_persona_trace_tolerates_malformed_duration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PERSONA_ENABLED", "1")
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    monkeypatch.setattr(
        hooks,
        "_build_persona_guidance_safely",
        lambda **_: {
            "enabled": True,
            "text": "Persona guidance:\n- Keep it concise.",
            "scene": "coding_plan",
            "risk_level": "medium",
            "tone": "direct",
            "duration_ms": "bad",
        },
    )

    result = hooks.before_prompt_build(
        {
            "session_id": "sess-persona-trace-bad-duration",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "persona trace should not break prompt build",
        }
    )

    assert result["persona_trace"]["guidance_latency_ms"] == 0.0
    assert result["persona_trace"]["scene"] == "coding_plan"
