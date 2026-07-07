from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge


def test_default_intent_pattern_maps_song_request_to_playback_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = runtime.search_policy(
        "给我唱首歌",
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
    )

    assert result["ok"] is True
    assert result["policy_suggestions"]
    suggestion = result["policy_suggestions"][0]
    assert suggestion["source"] == "intent_pattern"
    assert suggestion["event_type"] == "media_playback"
    assert suggestion["interpreted_intent"] == "播放音乐给用户听"
    assert "先判断播放出口和物理条件" in suggestion["execution_policy"]
    assert suggestion["success_criteria"] == "用户能听到或打开播放"


def test_event_outcome_correction_boosts_next_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    event = runtime.record_event(
        {
            "source": "manual",
            "user_phrase": "给我唱首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放一首用户能听见的歌",
            "goal": "让用户实际听到音乐",
            "constraints": ["不一定要创作", "需要考虑播放出口"],
            "physical_conditions": {
                "needs_audio_output": True,
                "available_channels": ["feishu_link", "local_speaker", "browser", "audio_file"],
                "missing_info": ["song_name", "playback_target"],
            },
            "action_path": ["询问歌曲名", "确认播放方式", "寻找音源", "播放或发送可听链接", "确认用户是否听见"],
            "result": "corrected_understanding",
            "evidence": ["用户纠正：其实就是播放一首歌"],
            "verification": "用户能听见/能打开播放",
            "lesson": "不要把‘唱首歌’先理解为创作，应优先判断如何让声音到达用户",
            "next_policy": "遇到类似口语化请求，先判断现实交付路径和物理条件",
            "confidence": 0.92,
            "notify_policy": "ask_missing_info",
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "把播放音乐误判成创作任务",
            "correction_from_user": "其实就是播放一首歌，要考虑怎么让我听见",
            "policy_update": "media_playback 类请求先确认歌曲和播放出口",
        },
        scope=scope,
    )

    result = runtime.search_policy("放首歌", scope=scope)

    assert result["ok"] is True
    assert result["matched_event_type"] == "media_playback"
    assert result["policy_suggestions"][0]["source"] == "event_outcome"
    assert result["policy_suggestions"][0]["outcome"] == "bad"
    assert result["policy_suggestions"][0]["policy_update"] == "media_playback 类请求先确认歌曲和播放出口"
    assert result["policy_suggestions"][0]["next_policy"] == "遇到类似口语化请求，先判断现实交付路径和物理条件"
    assert result["policy_suggestions"][0]["score"] > result["policy_suggestions"][1]["score"]


def test_upsert_intent_pattern_persists_across_runtime_reopen(tmp_path) -> None:
    scope = {"agent_id": "hongtu", "workspace_id": "research", "user_id": "darrow"}
    runtime = Runtime.create(root=tmp_path)
    runtime.upsert_intent_pattern(
        {
            "pattern": "最近 GitHub 最火|GitHub 热门项目",
            "default_event_type": "trending_search",
            "interpreted_intent": "搜索指定时间窗口内增长最快或讨论热度最高的 GitHub 项目",
            "execution_policy": ["先说明 trending 口径", "再按时间范围和排序条件检索"],
            "success_criteria": "返回结果附带时间窗口、排序口径和可验证链接",
        },
        scope=scope,
    )
    runtime.close()

    reopened = Runtime.create(root=tmp_path)
    result = reopened.search_policy("搜索最近 GitHub 最火", scope=scope)

    assert result["policy_suggestions"][0]["event_type"] == "trending_search"
    assert result["policy_suggestions"][0]["interpreted_intent"].startswith("搜索指定时间窗口")


def test_rpc_exposes_event_memory_policy_api(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    response = bridge.handle(
        {
            "method": "memory.searchPolicy",
            "params": {
                "query": "最高星项目",
                "scope": scope,
                "limit": 3,
            },
        }
    )

    assert response["ok"] is True
    assert response["result"]["policy_suggestions"][0]["event_type"] == "github_star_ranking"


def test_recall_explanation_surfaces_policy_suggestions_before_text_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.memory.ingest(
        text="唱首歌 can also mean a creative lyric request in a generic chat transcript.",
        memory_type="conversation",
        title="Generic song chat",
        scope=scope,
        force_capture=True,
    )

    bundle = runtime.memory.recall(
        query="给我唱首歌",
        scope=scope,
        task_context={"task_type": "chat.reply"},
        limit=3,
    )

    suggestions = bundle.explanation["policy_suggestions"]
    assert suggestions[0]["event_type"] == "media_playback"
    assert suggestions[0]["success_criteria"] == "用户能听到或打开播放"
    assert bundle.explanation["policy_first"] is True


def test_openclaw_agent_end_records_event_outcome_for_next_policy_search(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    result = hooks.on_agent_end(
        {
            "session_id": "sess-policy",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_messages": [{"content": "请帮我巡检 OpenClaw 队列"}],
            "assistant_messages": [{"content": "Summary: OpenClaw 队列已恢复。"}],
            "task_context": {
                "event_type": "operational_check",
                "interpreted_intent": "巡检 OpenClaw 队列并处理卡住任务",
                "goal": "恢复队列处理",
                "physical_conditions": {"gateway": "openclaw", "queue_depth": 2},
                "environment": {"host": "honxin"},
                "verification": "队列恢复，watchdog 日志无新增 stuck session",
            },
            "tools": ["openclaw_status", "systemctl"],
            "action_path": ["检查队列", "查看日志", "重启 gateway", "复查状态"],
            "outcome": {
                "success": True,
                "notes": "queue healthy after restart",
                "verified": True,
                "verification": "队列恢复，watchdog 日志无新增 stuck session",
            },
        }
    )

    assert result["event"]["user_phrase"] == "请帮我巡检 OpenClaw 队列"
    assert result["event"]["interpreted_intent"] == "巡检 OpenClaw 队列并处理卡住任务"
    assert result["event"]["tools"] == ["openclaw_status", "systemctl"]
    assert result["event"]["physical_conditions"]["queue_depth"] == 2
    assert result["outcome"]["outcome"] == "good"

    policy = runtime.search_policy("巡检 OpenClaw 队列", scope=scope)
    suggestion = policy["policy_suggestions"][0]
    assert suggestion["source"] == "event_outcome"
    assert suggestion["event_type"] == "operational_check"
    assert suggestion["outcome"] == "good"
    assert suggestion["tools"] == ["openclaw_status", "systemctl"]
    assert suggestion["verification"] == "队列恢复，watchdog 日志无新增 stuck session"


def test_openclaw_task_end_user_correction_records_bad_outcome_and_intent_pattern(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    result = hooks.on_task_end(
        {
            "session_id": "sess-correction",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_messages": [
                {"content": "给我唱一段"},
                {"content": "不对，不是让你写歌词，我是要能听见"},
            ],
            "assistant_messages": [{"content": "我写了一段歌词。"}],
            "task_context": {"interpreted_intent": "创作歌词", "goal": "生成歌词文本"},
            "outcome": {"success": True, "notes": "generated lyrics"},
        }
    )

    assert result["event"]["user_phrase"] == "给我唱一段"
    assert result["event"]["event_type"] == "media_playback"
    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["correction_from_user"] == "不对，不是让你写歌词，我是要能听见"
    assert "播放" in result["outcome"]["policy_update"]
    assert result["pattern"]["default_event_type"] == "media_playback"

    policy = runtime.search_policy("给我唱一段", scope=scope)
    assert any(
        item["source"] == "event_outcome"
        and item["outcome"] == "bad"
        and item["correction_from_user"] == "不对，不是让你写歌词，我是要能听见"
        for item in policy["policy_suggestions"]
    )
    assert any(
        item["source"] == "intent_pattern"
        and item["event_type"] == "media_playback"
        and "播放" in item["interpreted_intent"]
        for item in policy["policy_suggestions"]
    )


def test_openclaw_session_end_marks_success_without_verification_as_missing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    result = hooks.on_session_end(
        {
            "session_id": "sess-no-verification",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_messages": [{"content": "整理今天的会议纪要"}],
            "assistant_messages": [{"content": "Summary: 会议纪要已整理。"}],
            "task_context": {
                "event_type": "meeting_notes",
                "interpreted_intent": "整理会议纪要",
                "goal": "产出可复用会议纪要",
            },
            "outcome": {"success": True, "notes": "done"},
        }
    )

    assert result["event"]["user_phrase"] == "整理今天的会议纪要"
    assert result["outcome"]["outcome"] == "verification_missing"
    assert "verification" in result["outcome"]["reason"]

    policy = runtime.search_policy("整理今天的会议纪要", scope=scope)
    assert policy["policy_suggestions"][0]["source"] == "event_outcome"
    assert policy["policy_suggestions"][0]["outcome"] == "verification_missing"


def test_openclaw_agent_end_success_without_verification_does_not_feed_replay_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    result = hooks.on_agent_end(
        {
            "session_id": "sess-agent-no-verification",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_messages": [{"content": "整理一下今天的状态"}],
            "assistant_messages": [{"content": "Summary: 已整理。"}],
            "task_context": {
                "event_type": "status_summary",
                "interpreted_intent": "整理状态",
            },
            "outcome": {"success": True, "notes": "done"},
        }
    )

    assert result["outcome"]["outcome"] == "not_recorded"
    assert result["outcome"]["reason"] == "agent_end_success_without_explicit_verification"
    assert result["outcome"]["recorded"] is False
    assert result["outcome"]["verification"] == ""
    assert "outcome_trace" not in result

    rows = runtime.store.sqlite.conn.execute(
        "SELECT id FROM event_outcomes WHERE event_id = ?",
        (result["event"]["id"],),
    ).fetchall()
    assert rows == []

    policy = runtime.search_policy("整理一下今天的状态", scope=scope)
    assert all(item["source"] != "event_outcome" for item in policy["policy_suggestions"])
