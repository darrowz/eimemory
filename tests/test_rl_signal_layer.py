from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.evaluation.reward import RewardEngine
from eimemory.governance.closed_loop import post_experience_hook
from eimemory.governance.rl_policy import RLPolicy
from eimemory.storage.replay_buffer import ReplayBuffer


def test_reward_engine_scores_success_positive_and_failure_negative() -> None:
    engine = RewardEngine()

    good = engine.compute(
        experience={"task_type": "chat.reply"},
        eval_result={"ok": True, "recall_quality": 0.4, "primary_label": "success"},
        outcome={"success": True, "cost": 0.2},
    )
    bad = engine.compute(
        experience={"task_type": "chat.reply"},
        eval_result={"ok": False, "primary_label": "missing_tool_call"},
        outcome={"success": False, "cost": 0.3},
    )

    assert good["reward"] > 0
    assert good["components"]["task_success"] == 2.0
    assert bad["reward"] < 0
    assert bad["components"]["failure_penalty"] < 0


def test_replay_buffer_persists_scoped_transition_and_samples_only_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    buffer = ReplayBuffer(runtime.store)
    scope_a = {"tenant_id": "tenant-a", "agent_id": "eibrain", "workspace_id": "robot", "user_id": "alice"}
    scope_b = {"tenant_id": "tenant-b", "agent_id": "eibrain", "workspace_id": "robot", "user_id": "bob"}

    transition_a = buffer.add_transition(
        state={"trace": "a"},
        action={"id": "inspect_logs", "type": "tool.routing"},
        reward={"reward": 1.2},
        next_state={"ok": True},
        scope=scope_a,
        source_record_id="trace-a",
    )
    buffer.add_transition(
        state={"trace": "b"},
        action={"id": "inspect_logs", "type": "tool.routing"},
        reward={"reward": -1.0},
        next_state={"ok": False},
        scope=scope_b,
        source_record_id="trace-b",
    )

    assert transition_a.kind == "rl_transition"
    sampled = buffer.sample(scope=scope_a, k=10)
    assert [item.content["state"]["trace"] for item in sampled] == ["a"]
    assert sampled[0].meta["source_record_id"] == "trace-a"


def test_rl_policy_updates_value_and_selects_highest_policy_value(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    policy = RLPolicy(runtime.store, alpha=0.5)

    updated = policy.update(
        state={"task_type": "ops.inspect"},
        action={"id": "inspect_logs", "type": "tool.routing", "value": 0.0},
        reward={"reward": 2.0},
        scope=scope,
    )
    selected = policy.select_action(
        {
            "possible_actions": [
                {"id": "reply_from_memory", "type": "tool.routing", "value": 0.4},
                {"id": "inspect_logs", "type": "tool.routing", "value": 0.0},
            ]
        },
        scope=scope,
    )

    assert updated["value"] == 1.0
    assert selected["id"] == "inspect_logs"
    assert selected["policy_value"] == 1.0


def test_closed_loop_records_reward_transition_and_policy_update(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.generate_learning_thoughts = lambda **kwargs: {"ok": True, "thoughts": []}  # type: ignore[method-assign]
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    outcome = runtime.record_outcome_trace(
        {
            "trace_id": "rl-trace-1",
            "task_type": "ops.inspect",
            "input_summary": "Inspect service state",
            "outcome": {"status": "bad"},
            "expected_tools": ["ssh"],
            "selected_tools": [],
            "actions": [{"type": "reply"}],
            "cost": 0.1,
        },
        scope=scope,
    )

    report = post_experience_hook(runtime, outcome, scope)
    transitions = runtime.store.list_records(kinds=["rl_transition"], scope=scope, limit=10)
    values = runtime.store.list_records(kinds=["rl_policy_value"], scope=scope, limit=10)

    assert report["rl"]["reward"]["reward"] < 0
    assert report["rl"]["reward"]["components"]["cost"] == -0.1
    assert transitions[0].content["source_record_id"] == outcome["record_id"]
    assert transitions[0].content["action"]["type"] == "experience_feedback"
    assert values[0].meta["action_key"].startswith("experience_feedback:")
