from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_evolution import run_autonomous_evolution
from eimemory.governance.policy_replay import evaluate_safe_action_gate
from eimemory.governance.policy_trust import classify_outcome_source


def test_classify_outcome_source_includes_expected_values() -> None:
    assert (
        classify_outcome_source(
            outcome={
                "correction_from_user": "",
                "verification": "",
                "source_trust": "trusted_hook",
            }
        )
        == "trusted_hook"
    )
    assert (
        classify_outcome_source(
            outcome={
                "correction_from_user": "",
                "verification": "",
                "source": "web_hypothesis",
            }
        )
        == "web_external"
    )
    assert (
        classify_outcome_source(
            outcome={
                "correction_from_user": "",
                "verification": "",
                "source": "agent",
            }
        )
        == "agent_inferred"
    )


def test_agent_inferred_outcome_is_replay_blocked_for_apply(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_agent_inferred",
            "timestamp": "2026-05-31T01:00:00+08:00",
            "source": "manual",
            "user_phrase": "你又误操作了",
            "event_type": "repair",
            "interpreted_intent": "恢复服务",
            "goal": "服务恢复并验证",
            "verification": "",
            "confidence": 0.86,
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_agent_inferred",
        {
            "outcome": "bad",
            "reason": "只执行了重启，没有确认日志",
            "correction_from_user": "",
            "policy_update": "先查看日志和状态再低风险修复",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)

    assert report["applied_count"] == 0
    assert report["experiments"][0]["passed"] is False
    assert report["experiments"][0]["trusted_gate"]["ok"] is False
    assert report["experiments"][0]["trusted_gate"]["source_trust"] == "agent_inferred"


def test_user_explicit_correction_with_verification_passes_trust_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_policy_user_ok",
            "timestamp": "2026-05-31T01:05:00+08:00",
            "source": "manual",
            "user_phrase": "给我唱首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音乐",
            "goal": "用户能听见",
            "verification": "用户能听见",
            "confidence": 0.91,
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_policy_user_ok",
        {
            "outcome": "bad",
            "reason": "没有确认歌曲和播放路径",
            "correction_from_user": "先确认播放入口和歌曲",
            "policy_update": "播放请求先确认歌曲和播放出口，不要默认创作",
            "verification": "播放成功后复核",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)

    assert report["applied_count"] == 1
    assert report["experiments"][0]["passed"] is True
    assert report["experiments"][0]["trusted_gate"]["ok"] is True
    assert report["experiments"][0]["trusted_gate"]["source_trust"] == "user_explicit"
    assert report["applied_patches"]


def test_web_hypothesis_is_replay_only(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    report = run_autonomous_evolution(
        runtime,
        scope=scope,
        apply=True,
        web_hypotheses=[
            {
                "trigger": "hybrid retrieval",
                "event_type": "trend_tracking",
                "policy_update": "Hybrid 检索和 reranking 提升 recall。",
                "replay_hints": [
                    {
                        "query": "hybrid retrieval",
                        "expected_text": ["reduce noisy recall"],
                    }
                ],
            }
        ],
    )

    assert report["applied_count"] == 0
    assert report["experiments"][0]["trusted_gate"]["ok"] is False
    assert report["experiments"][0]["trusted_gate"]["source_trust"] == "web_external"


def test_negative_replay_signal_blocks_apply(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_policy_negative",
            "timestamp": "2026-05-31T01:10:00+08:00",
            "source": "manual",
            "user_phrase": "这条命令没响应",
            "event_type": "repair",
            "interpreted_intent": "修复故障",
            "goal": "服务恢复",
            "verification": "已恢复",
            "confidence": 0.82,
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_policy_negative",
        {
            "outcome": "bad",
            "reason": "恢复路径误判",
            "correction_from_user": "不是这个意思，别这样，只要先看日志和状态就行",
            "policy_update": "恢复前先查看日志和状态",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)

    assert report["applied_count"] == 0
    assert report["experiments"][0]["replay_gate"]["ok"] is False
    assert report["experiments"][0]["replay_gate"]["blocked_reason"] == "negative_replay_signal"


def test_regression_seed_blocks_highest_star_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_policy_regression",
            "timestamp": "2026-05-31T01:15:00+08:00",
            "source": "manual",
            "user_phrase": "给我最高星项目",
            "event_type": "github_star_ranking",
            "interpreted_intent": "查找 GitHub 最高星项目",
            "goal": "返回最高星项目",
            "verification": "返回结果并说明口径",
            "confidence": 0.88,
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_policy_regression",
        {
            "outcome": "bad",
            "reason": "把 trend 和最高星混淆",
            "correction_from_user": "最高星项目要按 stars 总量排序，不要最近趋势",
            "policy_update": "返回最高星项目时要区分 stars 总量和趋势",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)

    assert report["applied_count"] == 0
    assert report["experiments"][0]["replay_gate"]["ok"] is False
    assert report["experiments"][0]["replay_gate"]["blocked_reason"] == "regression_seed_pattern"
    assert "最高星项目" in report["experiments"][0]["replay_gate"]["signals"][0]


def test_safe_action_gate_blocks_high_risk_action_text(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_policy_high_risk",
            "timestamp": "2026-05-31T01:20:00+08:00",
            "source": "manual",
            "user_phrase": "修复故障",
            "event_type": "repair",
            "interpreted_intent": "恢复服务",
            "goal": "服务可用",
            "verification": "状态恢复",
            "confidence": 0.89,
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_policy_high_risk",
        {
            "outcome": "bad",
            "reason": "当前动作不够谨慎",
            "correction_from_user": "先做只读排查，不要删除日志",
            "policy_update": "恢复前先只读查看日志和状态，暂不删除缓存",
            "verification": "确认日志和状态可读",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)

    assert report["applied_count"] == 0
    assert report["experiments"][0]["safe_action_gate"]["ok"] is False
    assert "destructive_change" in report["experiments"][0]["safe_action_gate"]["blocked_categories"]


def test_safe_action_gate_blocks_high_risk_execution_commands() -> None:
    report = evaluate_safe_action_gate(
        patch={
            "pattern": "safe looking read-only repair",
            "interpreted_intent": "inspect service state before replying",
            "execution_policy": ["Only inspect status and logs."],
            "deployment_commands": [["bash", "-lc", "rm -rf /tmp/eimemory-cache"]],
            "rollback_plan": {"commands": [["bash", "-lc", "shutdown now"]]},
        }
    )

    assert report["ok"] is False
    assert "destructive_change" in report["blocked_categories"]
    assert "system_disruption" in report["blocked_categories"]


def test_safe_action_gate_blocks_nested_code_patch_commands() -> None:
    report = evaluate_safe_action_gate(
        patch={
            "patch_type": "code_patch",
            "pattern": "safe looking direct repo patch",
            "interpreted_intent": "apply a verified source edit",
            "execution_policy": ["Run normal verification."],
            "code_patch": {
                "verification_commands": [["python", "-m", "pytest", "tests/test_target.py"]],
                "rollback_plan": {"commands": [["bash", "-lc", "rm -rf /tmp/eimemory-cache"]]},
                "deployment_commands": [["bash", "-lc", "shutdown now"]],
            },
        }
    )

    assert report["ok"] is False
    assert "destructive_change" in report["blocked_categories"]
    assert "system_disruption" in report["blocked_categories"]
