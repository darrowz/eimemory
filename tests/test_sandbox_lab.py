from __future__ import annotations

from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.governance.sandbox_lab import create_sandbox_experiment


def test_create_sandbox_experiment_is_candidate_only(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    experiment_id = create_sandbox_experiment(
        runtime,
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="research_1",
        candidate_kind="tool_route",
        candidate_patch={"route": "memory_first", "rule": "Check memory before web search."},
    )

    experiment = runtime.store.get_by_id(experiment_id)
    assert experiment is not None
    assert experiment.kind == "learning_experiment"
    assert experiment.status == "candidate"
    assert experiment.meta["candidate_kind"] == "tool_route"
    assert experiment.meta["authority_tier"] == "L1"
    assert Path(experiment.content["artifact_path"]).exists()
    assert runtime.store.list_records(kinds=["rule"], scope={"agent_id": "hongtu"}, limit=10) == []
