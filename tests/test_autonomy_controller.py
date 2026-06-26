from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.rl_policy import RLPolicy
import eimemory.governance.autonomy_controller as autonomy_controller


def test_autonomy_cycle_wraps_learning_with_policy_roi_and_dashboard(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    calls: dict[str, int] = {}

    def fake_learning(_runtime, **kwargs):
        calls["max_goals"] = kwargs["max_goals"]
        calls["max_promotions"] = kwargs["max_promotions"]
        return {
            "ok": True,
            "loop_id": "learn-test",
            "goal_count": kwargs["max_goals"],
            "replay_dataset": {
                "case_count": 4,
                "filtered_count": 3,
                "quality_score": 0.875,
                "target_pass_rate": 0.8,
            },
            "real_task_replay": {"verdict": "pass", "pass_rate": 1.0},
            "promotions": [{"applied": True}],
        }

    monkeypatch.setattr(autonomy_controller, "_legacy_run_autonomous_learning_cycle", fake_learning)
    monkeypatch.setattr(
        runtime,
        "build_learning_dashboard",
        lambda **_kwargs: {
            "ok": True,
            "report_type": "autonomous_learning_daily_dashboard",
            "period_type": "daily",
            "persisted_record_id": "dash_1",
            "output_path": "",
        },
    )

    report = autonomy_controller.run_autonomy_cycle(
        runtime,
        scope=scope,
        max_goals=9,
        policy={"max_daily_goals": 3, "min_replay_pass_rate_for_auto": 0.8},
    )

    assert report["ok"] is True
    assert report["report_type"] == "autonomy_cycle"
    assert calls["max_goals"] == 3
    assert calls["max_promotions"] == 3
    assert report["bounded_max_goals"] == 3
    assert report["rollout_radius"] == "honxin_single_scope"
    assert report["replay_quality"]["filtered_count"] == 3
    assert report["replay_quality"]["real_task_pass_rate"] == 1.0
    assert report["promotion_control"]["applied_count"] == 1
    assert report["dashboard"]["period_type"] == "daily"


def test_autonomy_cycle_bounds_zero_max_goals_to_one_not_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: dict[str, int] = {}

    def fake_learning(_runtime, **kwargs):
        calls["max_goals"] = kwargs["max_goals"]
        return {"ok": True, "replay_dataset": {}, "real_task_replay": {}, "promotions": []}

    monkeypatch.setattr(autonomy_controller, "_legacy_run_autonomous_learning_cycle", fake_learning)
    monkeypatch.setattr(runtime, "build_learning_dashboard", lambda **_kwargs: {"ok": True, "report_type": "autonomous_learning_daily_dashboard", "period_type": "daily"})

    report = autonomy_controller.run_autonomy_cycle(runtime, max_goals=0, policy={"max_daily_goals": 3})

    assert report["ok"] is True
    assert calls["max_goals"] == 1
    assert report["bounded_max_goals"] == 1


def test_autonomy_cycle_uses_rl_policy_to_select_conservative_behavior(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    calls: dict[str, int] = {}

    RLPolicy(runtime.store, alpha=1.0).update(
        state={"source": "autonomy.cycle"},
        action={"id": "run_autonomy_cycle", "type": "autonomy_cycle", "value": 0.0},
        reward={"reward": -1.0},
        scope=scope,
    )

    def fake_learning(_runtime, **kwargs):
        calls["max_goals"] = kwargs["max_goals"]
        return {"ok": True, "replay_dataset": {}, "real_task_replay": {}, "promotions": []}

    monkeypatch.setattr(autonomy_controller, "_legacy_run_autonomous_learning_cycle", fake_learning)
    monkeypatch.setattr(runtime, "build_learning_dashboard", lambda **_kwargs: {"ok": True, "report_type": "autonomous_learning_daily_dashboard", "period_type": "daily"})

    report = autonomy_controller.run_autonomy_cycle(runtime, scope=scope, max_goals=3, policy={"max_daily_goals": 3})

    assert report["ok"] is True
    assert calls["max_goals"] == 1
    assert report["bounded_max_goals"] == 1
    assert report["policy_decision"]["id"] == "conservative_autonomy_cycle"
    assert report["policy_decision"]["selected_by"] == "rl_policy.value_table"


def test_autonomy_cycle_smoke_skips_heavy_learning_and_keeps_policy_decision(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("smoke must not call heavy autonomous learning")

    monkeypatch.setattr(autonomy_controller, "_legacy_run_autonomous_learning_cycle", fail_if_called)

    report = autonomy_controller.run_autonomy_cycle(
        runtime,
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        max_goals=3,
        policy={"max_daily_goals": 3},
        smoke=True,
    )

    assert report["ok"] is True
    assert report["report_type"] == "autonomy_cycle"
    assert report["smoke"] is True
    assert report["policy_decision"]["id"] == "run_autonomy_cycle"
    assert report["replay_quality"]["case_count"] == 1
