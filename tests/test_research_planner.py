from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.research_planner import create_research_task, plan_research_tasks


def test_plan_research_tasks_for_recall_goal_skips_network_by_default() -> None:
    goal = {"title": "Improve memory recall", "question": "Recall missed preferences", "target_capability": "memory.recall"}

    tasks = plan_research_tasks(goal, source_policy={"network_enabled": False})

    assert {task["task_type"] for task in tasks} >= {"local_history_review", "benchmark_review", "tool_comparison"}
    assert all(not task["network"] for task in tasks)


def test_create_research_task_persists_idempotently(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    task = plan_research_tasks({"title": "Improve tool routing"}, {})[0]

    first = create_research_task(runtime, scope={"agent_id": "hongtu"}, loop_id="learn_test", goal_id="goal_1", task=task)
    second = create_research_task(runtime, scope={"agent_id": "hongtu"}, loop_id="learn_test", goal_id="goal_1", task=task)

    assert second == first
    assert len(runtime.store.list_records(kinds=["research_task"], scope={"agent_id": "hongtu"}, limit=10)) == 1
