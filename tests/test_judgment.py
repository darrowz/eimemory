from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.scheduler.jobs import run_nightly_jobs


def test_runtime_judgment_evaluation_summarizes_events_into_playbook(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    first_failure = runtime.record_event(
        {
            "id": "evt_repair_bad_1",
            "timestamp": "2026-05-29T20:00:00+00:00",
            "source": "heartbeat",
            "user_phrase": "OpenClaw 又没反应",
            "event_type": "repair",
            "interpreted_intent": "修复 OpenClaw 卡住",
            "goal": "恢复 OpenClaw 可响应状态",
            "action_path": ["看进程", "重启服务"],
            "verification": "",
            "confidence": 0.72,
        },
        scope=scope,
    )
    runtime.record_outcome(
        first_failure["id"],
        {
            "outcome": "bad",
            "reason": "只重启服务，没有确认根因",
            "correction_from_user": "先看日志和状态，别只做临时重启",
            "policy_update": "repair 请求先诊断日志、进程和最近变更，再低风险修复并验证",
        },
        scope=scope,
    )
    second_failure = runtime.record_event(
        {
            "id": "evt_repair_bad_2",
            "timestamp": "2026-05-29T21:00:00+00:00",
            "source": "nightly",
            "user_phrase": "又坏了",
            "event_type": "repair",
            "interpreted_intent": "处理重复故障",
            "goal": "避免同类故障反复出现",
            "action_path": ["重启服务"],
            "verification": "再次触发 heartbeat 应返回 ok",
            "confidence": 0.81,
        },
        scope=scope,
    )
    runtime.record_outcome(
        second_failure["id"],
        {
            "outcome": "bad",
            "reason": "重复临时修复，缺少可复现诊断路径",
            "policy_update": "重复 repair 失败时产出诊断路径和成功标准",
        },
        scope=scope,
    )
    reliable = runtime.record_event(
        {
            "id": "evt_repair_good",
            "timestamp": "2026-05-29T22:00:00+00:00",
            "source": "heartbeat",
            "user_phrase": "OpenClaw 没反应",
            "event_type": "repair",
            "interpreted_intent": "恢复服务",
            "goal": "让服务恢复并证明可用",
            "action_path": ["查 systemd 状态", "看最近错误日志", "重启服务", "运行健康检查"],
            "verification": "healthcheck 返回 ok",
            "confidence": 0.9,
        },
        scope=scope,
    )
    runtime.record_outcome(
        reliable["id"],
        {"outcome": "good", "reason": "诊断路径完整且验证成功"},
        scope=scope,
    )
    uncertain = runtime.record_event(
        {
            "id": "evt_noise_uncertain",
            "timestamp": "2026-05-29T23:00:00+00:00",
            "source": "heartbeat",
            "user_phrase": "",
            "event_type": "heartbeat",
            "interpreted_intent": "低置信度心跳噪声",
            "goal": "",
            "confidence": 0.2,
        },
        scope=scope,
    )
    runtime.record_outcome(uncertain["id"], {"outcome": "uncertain", "reason": "缺少上下文"}, scope=scope)

    report = runtime.run_judgment_evaluation(scope=scope, since="2026-05-29T19:00:00+00:00", limit=20)

    assert report["ok"] is True
    assert report["outcome_counts"] == {
        "good": 1,
        "bad": 2,
        "uncertain": 1,
        "verification_missing": 2,
    }
    assert report["repeated_failures"][0]["event_type"] == "repair"
    assert report["repeated_failures"][0]["count"] == 2
    assert {item["event_id"] for item in report["user_corrections"]} == {first_failure["id"]}
    assert report["reliable_paths"][0]["event_id"] == reliable["id"]
    assert report["noise_signals"][0]["event_id"] == uncertain["id"]

    entry = report["playbook_entries"][0]
    assert set(entry) == {
        "trigger",
        "policy",
        "evidence",
        "success_criteria",
        "source_event_ids",
        "confidence",
    }
    assert "repair" in entry["trigger"]
    assert "先诊断日志" in entry["policy"]
    assert first_failure["id"] in entry["source_event_ids"]
    assert second_failure["id"] in entry["source_event_ids"]


def test_runtime_judgment_evaluation_respects_since_limit_and_persists_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "kitchen", "user_id": "darrow"}
    runtime.record_event(
        {
            "id": "evt_old_tea",
            "timestamp": "2026-05-28T10:00:00+00:00",
            "source": "manual",
            "user_phrase": "泡茶失败",
            "event_type": "tea_service",
            "interpreted_intent": "泡一杯可喝的茶",
            "goal": "用户能喝到茶",
            "verification": "用户确认喝到茶",
            "confidence": 0.9,
        },
        scope=scope,
    )
    recent = runtime.record_event(
        {
            "id": "evt_recent_tea",
            "timestamp": "2026-05-29T10:00:00+00:00",
            "source": "manual",
            "user_phrase": "泡茶失败",
            "event_type": "tea_service",
            "interpreted_intent": "修正泡茶执行路径",
            "goal": "用户能喝到温度合适的茶",
            "action_path": ["确认茶种", "确认水温", "冲泡", "让用户确认"],
            "verification": "用户确认茶可喝",
            "confidence": 0.88,
        },
        scope=scope,
    )
    runtime.record_outcome(
        recent["id"],
        {
            "outcome": "bad",
            "reason": "没有确认茶种和水温",
            "correction_from_user": "先问茶种，再决定水温",
            "policy_update": "泡茶请求先确认茶种和水温，再执行冲泡并让用户确认",
        },
        scope=scope,
    )

    report = runtime.run_judgment_evaluation(
        scope=scope,
        since="2026-05-29T00:00:00+00:00",
        limit=1,
        persist_playbook=True,
    )
    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    policy = runtime.search_policy("泡茶失败", scope=scope, limit=5)

    assert report["scanned_event_count"] == 1
    assert report["source_event_ids"] == [recent["id"]]
    assert report["persisted"] is True
    assert report["persisted_record_id"]
    assert reflections[0].source == "eimemory.judgment_evaluation"
    assert reflections[0].content["report"]["playbook_entries"][0]["trigger"] == "tea_service: 泡茶失败"
    assert any(
        suggestion["source"] == "intent_pattern"
        and suggestion["event_type"] == "tea_service"
        and suggestion["success_criteria"] == "用户确认茶可喝"
        for suggestion in policy["policy_suggestions"]
    )


def test_nightly_jobs_runs_judgment_evaluation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_nightly_repair",
            "timestamp": "2026-05-29T12:00:00+00:00",
            "source": "heartbeat",
            "user_phrase": "服务又坏了",
            "event_type": "repair",
            "interpreted_intent": "修复服务",
            "goal": "恢复服务",
            "verification": "健康检查通过",
            "confidence": 0.84,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "临时修复，没有记录诊断步骤",
            "policy_update": "repair 请求必须记录诊断步骤和验证结果",
        },
        scope=scope,
    )

    report = run_nightly_jobs(runtime, scope=scope)
    persisted = runtime.store.get_by_id(report["judgment_evaluation"]["persisted_record_id"], scope=scope)

    assert report["judgment_evaluation"]["ok"] is True
    assert report["judgment_evaluation"]["persisted"] is True
    assert report["judgment_evaluation"]["playbook_entry_count"] >= 1
    assert persisted is not None
    assert persisted.meta["report_type"] == "judgment_evaluation"
