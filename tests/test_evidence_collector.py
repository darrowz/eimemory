from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_collector import collect
from eimemory.governance.research_planner import create_research_note


def test_collect_local_history_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="bad route", fix="memory first", scope=scope)

    evidence = collect({"task_type": "local_history_review"}, runtime=runtime, scope=scope)

    assert evidence
    assert evidence[0]["tier"] in {"T0", "T2"}
    assert evidence[0]["kind"] == "record"


def test_research_note_rejects_llm_only_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    with pytest.raises(ValueError, match="LLM synthesis"):
        create_research_note(
            runtime,
            scope={"agent_id": "hongtu"},
            loop_id="learn_test",
            learning_goal_id="goal_1",
            title="LLM only",
            summary="No real evidence",
            evidence=[{"tier": "T6", "kind": "llm_synthesis", "ref": "llm", "summary": "guess"}],
        )
