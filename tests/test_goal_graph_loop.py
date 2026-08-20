from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRevision,
)
from eimemory.models.records import ScopeRef


SCOPE = {
    "tenant_id": "tenant-goal-graph",
    "agent_id": "agent-goal-graph",
    "workspace_id": "goal-graph",
    "user_id": "user-goal-graph",
}
STAMP = "2020-08-20T00:00:00Z"


def _register_dynamic_goal_capability(runtime: Runtime) -> tuple[str, str]:
    """Install only this test's explicit v3 control-plane descriptors."""

    capability_id = "goal.graph.dynamic"
    definition = CapabilityDefinition(
        capability_id=capability_id,
        display_name="Dynamic Goal Graph",
        description="A test-local registry capability for goal planning.",
        owner="governance",
        created_at=STAMP,
        provenance={"source": "goal-graph-test"},
    )
    revision = CapabilityRevision(
        revision_id=f"{capability_id}:v1",
        capability_id=capability_id,
        contract={
            "input_schema": {"type": "object", "required": ["request"]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_is_traceable"],
            "failure_invariants": ["blocked_input"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=STAMP,
        provenance={"source": "goal-graph-test"},
    )
    binding = CapabilityBinding(
        binding_id=f"binding.{capability_id}:v1",
        capability_id=capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="goal-graph-local",
        implementation_digest="a" * 64,
        operations=("evaluate",),
        limits={"max_requests": 8},
        environment_fingerprint={"runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://goal-graph/advertisement.json",),
        provenance={"source": "goal-graph-test"},
        created_at=STAMP,
    )
    profile_key = "profile.goal.graph"
    profile = CapabilityProfile(
        profile_id=f"{profile_key}:v1",
        profile_key=profile_key,
        requirements={capability_id: {"minimum_maturity": "evaluated"}},
        created_at=STAMP,
        provenance={"source": "goal-graph-test"},
    )
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(binding, runtime_scope=SCOPE)
    runtime.capabilities.register_profile(profile, runtime_scope=SCOPE)
    return capability_id, profile_key


def test_goal_graph_builds_executable_tree_and_episode_events(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        capability_id, profile_key = _register_dynamic_goal_capability(runtime)
        report = runtime.build_goal_graph_loop(
            scope=SCOPE,
            max_goals=2,
            persist=True,
            capabilities=[capability_id],
            profile_key=profile_key,
        )

        assert report["ok"] is True
        assert report["loop_contract"]["invariant"] == "signal -> candidate -> gate -> apply -> observe -> score -> ledger -> active/rollback"
        assert report["root_goal_count"] == 1
        assert report["task_count"] >= 2
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
        capability_id, profile_key = _register_dynamic_goal_capability(runtime)
        report = runtime.build_goal_graph_loop(
            scope=SCOPE,
            max_goals=1,
            persist=False,
            capabilities=[capability_id],
            profile_key=profile_key,
        )
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


def test_goal_graph_blocks_when_dynamic_selection_is_empty(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_goal_graph_loop(
            scope=SCOPE,
            max_goals=1,
            persist=False,
            capabilities=["unregistered.goal"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert report["reason"] == "dynamic_goal_selection_empty"
    assert report["nodes"] == []


def test_goal_graph_legacy_mode_uses_only_its_explicit_cohort(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_goal_graph_loop(
            scope=SCOPE,
            max_goals=1,
            persist=True,
            capabilities=["legacy.goal.graph"],
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["legacy_compatibility"] is True
    assert report["root_goal_count"] == 1
    assert report["nodes"][0]["target_capability"] == "legacy.goal.graph"
    assert report["persisted_record_id"]
