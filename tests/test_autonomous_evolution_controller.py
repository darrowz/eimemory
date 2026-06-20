from __future__ import annotations

import sys

from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_evolution import run_autonomous_evolution


def test_autonomous_evolution_mines_bad_outcome_into_opportunity_and_replay(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_repair_bad",
            "timestamp": "2026-05-31T01:00:00+08:00",
            "source": "manual",
            "user_phrase": "OpenClaw 又没反应",
            "event_type": "repair",
            "interpreted_intent": "恢复 OpenClaw",
            "goal": "服务恢复并验证",
            "verification": "",
            "confidence": 0.84,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "只做临时重启，没有诊断日志",
            "correction_from_user": "先看日志和状态，别只重启",
            "policy_update": "repair 请求先诊断日志、状态和最近变更，再低风险修复并验证",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=False)

    assert report["ok"] is True
    assert report["report_type"] == "autonomous_evolution"
    assert report["opportunity_count"] == 1
    assert report["opportunities"][0]["opportunity_type"] == "intent_policy"
    assert report["opportunities"][0]["source"] == "event"
    assert report["replay_cases"][0]["query"] == "OpenClaw 又没反应"
    assert "先看日志" in " ".join(report["replay_cases"][0]["expected_text"])


def test_autonomous_evolution_applies_low_risk_intent_pattern_after_replay(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_media_bad",
            "timestamp": "2026-05-31T01:10:00+08:00",
            "source": "manual",
            "user_phrase": "给我唱首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音乐给用户听",
            "goal": "用户能听见或打开播放",
            "verification": "用户能听见或打开播放",
            "confidence": 0.91,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "把播放请求误判成创作歌词",
            "correction_from_user": "其实就是播放一首歌，要考虑怎么让我听见",
            "policy_update": "media_playback 请求先确认歌曲和播放出口，不要默认创作歌词",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)
    policy = runtime.search_policy("给我唱首歌", scope=scope)
    media_suggestions = [item for item in policy["policy_suggestions"] if item.get("event_type") == "media_playback"]
    intent_pattern_suggestions = [item for item in media_suggestions if item.get("source") == "intent_pattern"]

    assert report["applied_count"] == 1
    assert report["applied_patches"]
    assert report["applied_patches"][0]["patch_type"] == "intent_pattern"
    assert intent_pattern_suggestions
    assert "播放出口" in " ".join(intent_pattern_suggestions[0]["execution_policy"])


def test_autonomous_evolution_persists_reflection_report_when_requested(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_media_bad_reflection",
            "timestamp": "2026-05-31T01:20:00+08:00",
            "source": "manual",
            "user_phrase": "再给我来一首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音乐",
            "goal": "用户能听见",
            "verification": "用户能听见",
            "confidence": 0.87,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "默认理解成创作指令",
            "correction_from_user": "这是直接播放，不是创作",
            "policy_update": "先确认播放目标和音频输出再播放",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, persist_report=True)
    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    persisted = runtime.store.get_by_id(report["persisted_record_id"]) if report["persisted"] else None

    assert report["persisted"] is True
    assert report["persisted_record_id"]
    assert persisted is not None
    assert persisted.kind == "reflection"
    assert persisted.meta["report_type"] == "autonomous_evolution"
    assert reflections
    assert reflections[0].record_id == report["persisted_record_id"]


def test_autonomous_evolution_reports_patch_experiments(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_media_experiment",
            "timestamp": "2026-05-31T01:25:00+08:00",
            "source": "manual",
            "user_phrase": "放首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音乐",
            "goal": "用户能听见",
            "verification": "用户能听见",
            "confidence": 0.89,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "只生成了歌词，没有考虑音频出口",
            "correction_from_user": "先确认歌曲和播放方式",
            "policy_update": "media_playback 请求先确认歌曲和播放出口",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=False)

    assert report["experiments"]
    assert report["experiments"][0]["opportunity_id"] == report["opportunities"][0]["opportunity_id"]
    assert report["experiments"][0]["evaluation"]["ok"] is True
    assert report["passed_experiment_count"] == 1


def test_web_scout_hypothesis_becomes_replay_only_opportunity(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    report = run_autonomous_evolution(
        runtime,
        scope=scope,
        apply=True,
        web_hypotheses=[
            {
                "id": "web_hyp_rag",
                "source": "web_scout",
                "risk_level": "medium",
                "source_url": "https://example.com/rag",
                "candidate_policy": {
                    "title": "Hybrid retrieval and reranking",
                    "policy_update": "Use hybrid retrieval and reranking to reduce noisy recall.",
                    "confidence_hint": 0.68,
                },
                "replay_hints": [
                    {
                        "query": "Hybrid retrieval and reranking",
                        "expected_text": ["reduce noisy recall"],
                        "source_url": "https://example.com/rag",
                    }
                ],
            }
        ],
    )

    assert report["opportunity_count"] == 1
    assert report["opportunities"][0]["source"] == "web_hypothesis"
    assert report["replay_cases"][0]["query"] == "Hybrid retrieval and reranking"
    assert report["replay_cases"][0]["expected_text"] == ["reduce noisy recall"]
    assert report["experiments"][0]["evaluation"]["ok"] is False
    assert report["experiments"][0]["evaluation"]["blocked_reason"] == "unsupported_patch_type"
    assert report["applied_count"] == 0
    assert runtime.search_policy("Hybrid retrieval", scope=scope)["policy_suggestions"] == []


def test_web_hypotheses_medium_and_high_risk_not_directly_applied(tmp_path) -> None:
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
                "policy_update": "Hybrid retrieval 和 reranking 降低噪声。",
                "risk_level": "medium",
            },
            {
                "trigger": "delete cache before query",
                "event_type": "maintenance",
                "policy_update": "默认删除缓存再查询。",
                "risk_level": "high",
            },
        ],
    )
    policy = runtime.search_policy("hybrid retrieval", scope=scope)

    assert report["apply"] is True
    assert report["applied_count"] == 0
    assert report["opportunity_count"] == 2
    assert not report["applied_patches"]
    assert any(item["risk_level"] == "medium" for item in report["opportunities"])
    assert any(item["risk_level"] == "high" for item in report["opportunities"])
    assert not policy["policy_suggestions"]


def test_autonomous_evolution_applies_structured_code_patch_from_bad_outcome(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("VALUE = 'broken'\n", encoding="utf-8")
    event = runtime.record_event(
        {
            "id": "evt_code_bad",
            "timestamp": "2026-06-19T09:00:00+08:00",
            "source": "autonomous_test",
            "user_phrase": "fix failing module",
            "event_type": "code.implementation",
            "interpreted_intent": "fix runtime bug",
            "goal": "module imports with corrected value",
            "verification": "python import check passes",
            "confidence": 0.92,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "Traceback: module returned broken value",
            "correction_from_user": "patch module.py so VALUE is fixed",
            "policy_update": "apply a direct code patch and verify it imports",
            "source_trust": "system_verified",
            "verification": "pytest-style import command is provided",
            "code_patch": {
                "summary": "Fix broken VALUE constant",
                "repo_root": str(repo),
                "apply_to_repo": True,
                "deploy_to_production": False,
                "commit_to_repo": False,
                "allowed_files": ["module.py"],
                "file_updates": [{"path": "module.py", "content": "VALUE = 'fixed'\n"}],
                "verification_commands": [
                    [
                        sys.executable,
                        "-c",
                        "import pathlib; ns={}; exec(pathlib.Path('module.py').read_text(encoding='utf-8'), ns); assert ns['VALUE'] == 'fixed'",
                    ]
                ],
            },
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True, max_apply=1)

    assert report["ok"] is True
    assert report["applied_count"] == 1
    assert report["applied_patches"][0]["patch_type"] == "code_patch"
    assert report["applied_patches"][0]["side_effect"]["adapter"] == "direct_repo_patch"
    assert target.read_text(encoding="utf-8") == "VALUE = 'fixed'\n"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    assert report["rollout_ledger_ids"] == [report["applied_patches"][0]["rollout_ledger_id"]]
    assert any(item["promotion_id"] == report["applied_patches"][0]["promotion_id"] for item in ledger)
