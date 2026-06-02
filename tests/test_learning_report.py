from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.learning_report import build_learning_daily_report
from eimemory.governance.learning_state import append_learning_record_once


def test_learning_daily_report_persists_short_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    append_learning_record_once(
        runtime,
        kind="learning_goal",
        title="Improve health checks",
        summary="Health checks should use compact endpoints.",
        scope=scope,
        loop_id="learn_test",
        step_name="goal",
        semantic_key="goal-health",
    )
    append_learning_record_once(
        runtime,
        kind="promotion_request",
        title="Promotion applied: compact health",
        summary="Compact health endpoint policy applied.",
        scope=scope,
        loop_id="learn_test",
        step_name="promotion",
        semantic_key="promotion-health",
        status="promoted",
        content={"candidate_id": "candidate-1"},
    )
    append_learning_record_once(
        runtime,
        kind="promotion_request",
        title="Promotion blocked: deploy rollout",
        summary="Deployment rollout stayed blocked.",
        scope=scope,
        loop_id="learn_test",
        step_name="promotion",
        semantic_key="promotion-blocked",
        status="blocked",
        content={"side_effect": {"blocked_reason": "unsupported_rollout_adapter:deployment_rollout"}},
    )

    report = build_learning_daily_report(runtime, scope=scope, report_date="2099-01-01", persist=True)

    assert report["ok"] is True
    assert report["persisted_record_id"]
    assert report["learned"] == ["ops.health: Health checks should use compact endpoints."]
    assert report["applied"]
    assert report["blocked"]
    assert len(report["summary"]) < 700
    stored = runtime.store.get_by_id(report["persisted_record_id"], scope=scope)
    assert stored.meta["report_type"] == "autonomous_learning_daily_report"


def test_learning_daily_report_skips_tool_message_noise(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    noisy = (
        '{"type":"toolCall","id":"call_1","name":"message","arguments":'
        '{"action":"send","message":"assistant: this is a long historical response user: with old context that should not become a learning report item"}}'
    )
    append_learning_record_once(
        runtime,
        kind="world_signal",
        title="Noisy tool envelope",
        summary=noisy,
        scope=scope,
        loop_id="learn_test",
        step_name="world_watch",
        semantic_key="noise-tool-message",
        meta={"target_capability": "tool.routing"},
    )
    append_learning_record_once(
        runtime,
        kind="world_signal",
        title="Health timeout signal",
        summary="RPC health endpoint timed out because it returned a large daily digest payload.",
        scope=scope,
        loop_id="learn_test",
        step_name="world_watch",
        semantic_key="clean-health-signal",
    )

    report = build_learning_daily_report(runtime, scope=scope, report_date="2099-01-01", persist=False)

    assert report["noise_skipped_count"] == 1
    assert report["learned"] == ["ops.health: RPC health endpoint timed out because it returned a large daily digest payload."]
    assert "toolCall" not in report["summary"]
    assert "assistant:" not in report["summary"]
