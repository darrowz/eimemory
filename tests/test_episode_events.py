from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.episode_events import record_task_episode


SCOPE = {"agent_id": "hongtu", "workspace_id": "episode-graph", "user_id": "darrow"}


def test_successful_task_episode_does_not_create_synthetic_failure_edge(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = record_task_episode(
        runtime,
        scope=SCOPE,
        task={"task_id": "task-success", "title": "Successful graph closure"},
        outcome={"ok": True, "status": "success"},
        failures=[],
    )

    assert result["ok"] is True
    assert result["failure_count"] == 0
    edges = runtime.store.list_memory_edges(scope=SCOPE, record_ids=[result["record_id"]], limit=50)
    assert "episode_failure_or_risk_trace" not in {edge.reason for edge in edges}


def test_failed_task_episode_gets_failure_edge_when_failures_omitted(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = record_task_episode(
        runtime,
        scope=SCOPE,
        task={"task_id": "task-failed", "title": "Failed graph closure"},
        outcome={"ok": False, "status": "failed", "reason": "verification failed"},
    )

    assert result["ok"] is True
    assert result["failure_count"] == 1
    edges = runtime.store.list_memory_edges(scope=SCOPE, record_ids=[result["record_id"]], limit=50)
    assert "episode_failure_or_risk_trace" in {edge.reason for edge in edges}
