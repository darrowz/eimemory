from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.self_model import build_self_model


def test_build_self_model_extracts_weakness_from_reflections(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="tool.routing",
        miss="Used web search when local memory already had the answer",
        fix="Check memory before web search for stable personal facts",
        scope=scope,
    )

    model = build_self_model(runtime, scope=scope)

    assert model["weaknesses"][0]["kind"] == "tool.routing"
    assert model["weaknesses"][0]["capability"] == "tool.routing"
    assert "Check memory before web search" in model["weaknesses"][0]["lesson"]


def test_build_self_model_persists_model_and_weakness_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(tag="memory.recall", miss="Recall missed preference", fix="Prefer preference memory", scope=scope)

    build_self_model(runtime, scope=scope, loop_id="learn_test", persist=True)

    assert runtime.store.list_records(kinds=["capability_model"], scope=scope, limit=10)
    assert runtime.store.list_records(kinds=["weakness"], scope=scope, limit=10)
