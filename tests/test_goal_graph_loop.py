from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


SCOPE = {"agent_id": "agent-goal-graph", "workspace_id": "goal-graph"}


def test_goal_graph_builds_executable_tree_and_episode_events(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_goal_graph_loop(
            scope=SCOPE,
            max_goals=2,
            persist=True,
            capabilities=["memory.recall", "tool.routing"],
        )

        assert report["ok"] is True
        assert report["loop_contract"]["invariant"] == "signal -> candidate -> gate -> apply -> observe -> score -> ledger -> active/rollback"
        assert report["root_goal_count"] == 2
        assert report["task_count"] >= 4
        assert report["episode_event_count"] == report["task_count"]
        assert report["persisted_record_id"]

        required_fields = {
            "goal_id",
            "parent_goal_id",
            "root_goal_id",
            "status",
            "success_criteria",
            "evidence_refs",
            "task_refs",
            "candidate_refs",
            "reward",
            "ledger_refs",
            "rollback_refs",
        }
        assert all(required_fields.issubset(node) for node in report["nodes"])
        assert {node["node_type"] for node in report["nodes"]} >= {"root_goal", "sub_goal", "task"}
        assert all(node["root_goal_id"] for node in report["nodes"])
        assert all(node["success_criteria"] for node in report["nodes"])

        graph_record = runtime.store.get_by_id(report["persisted_record_id"], scope=ScopeRef.from_dict(SCOPE))
        assert graph_record is not None
        assert graph_record.kind == "reflection"
        assert graph_record.status == "active"
        assert graph_record.meta["report_type"] == "goal_graph_loop"
        assert graph_record.content["loop_contract"]["complete_capability_requires"] == [
            "replay",
            "ledger",
            "observe",
            "rollback",
        ]

        episode_records = runtime.store.list_records(kinds=["memory"], scope=SCOPE, limit=20)
        episode_records = [record for record in episode_records if record.meta.get("memory_type") == "task_episode"]
        assert len(episode_records) == report["task_count"]
        first_episode = episode_records[0]
        assert first_episode.content["episode"]["event_id"]
        assert first_episode.content["episode"]["entities"]
        assert first_episode.content["episode"]["decisions"]
        assert first_episode.content["episode"]["artifacts"]
        assert "task" in first_episode.content["episode"]
    finally:
        runtime.close()


def test_goal_graph_observation_closes_node_with_reward_and_ledger_refs(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_goal_graph_loop(scope=SCOPE, max_goals=1, persist=False, capabilities=["memory.recall"])
        task_node = next(node for node in report["nodes"] if node["node_type"] == "task")

        observed = runtime.observe_goal_graph_node(
            graph=report,
            node_id=task_node["goal_id"],
            status="active",
            reward=0.88,
            ledger_refs=["cap_score_memory_recall"],
            rollback_refs=["rollback-memory-recall"],
            persist=True,
            scope=SCOPE,
        )

        assert observed["ok"] is True
        updated = next(node for node in observed["graph"]["nodes"] if node["goal_id"] == task_node["goal_id"])
        assert updated["status"] == "active"
        assert updated["reward"] == 0.88
        assert updated["ledger_refs"] == ["cap_score_memory_recall"]
        assert updated["rollback_refs"] == ["rollback-memory-recall"]
        assert observed["persisted_record_id"]
    finally:
        runtime.close()
