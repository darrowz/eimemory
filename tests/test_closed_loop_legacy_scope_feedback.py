"""Closed-loop feedback stays in the outcome's exact scope (legacy aliases too)."""
from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.closed_loop import post_experience_hook


def test_legacy_scope_outcome_completes_closed_loop_projection(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.generate_learning_thoughts = lambda **kwargs: {"ok": True, "thoughts": []}  # type: ignore[method-assign]
        scope = {"agent_id": "eibrain", "workspace_id": "robot"}
        outcome = runtime.record_outcome_trace(
            {"trace_id": "legacy-1", "task_type": "ops.inspect", "input_summary": "Inspect service state",
             "outcome": {"status": "bad"}, "expected_tools": ["ssh"], "selected_tools": [],
             "actions": [{"type": "reply"}], "cost": 0.1},
            scope=scope,
        )
        report = post_experience_hook(runtime, outcome, scope)
        assert report.get("error") is None
        assert report["status"] == "completed"
        assert report["event_graph"]["ok"] is True
        assert report["rl"]["ok"] is True
        assert report["rl"]["reward"]["reward"] < 0
    finally:
        runtime.close()
