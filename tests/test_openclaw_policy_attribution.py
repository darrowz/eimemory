from __future__ import annotations

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle


def _audit_bound_policy(hooks, event, policy_id):
    return hooks._audit_prompt_recall(event=event, injected=False, bundle=RecallBundle(
        items=[], rules=[], reflections=[], confidence=0.9, next_action_hint="",
        explanation={"policy_suggestion_ids": [policy_id], "policy_sources": ["intent_pattern"]},
    ))


def test_delayed_terminal_uses_its_own_audit_after_newer_session_task(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        hooks = OpenClawMemoryHooks(runtime)
        event = {"session_id": "shared", "agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"}
        first = _audit_bound_policy(hooks, {**event, "turn_id": "A"}, "policy-A")
        _audit_bound_policy(hooks, {**event, "turn_id": "B"}, "policy-B")
        attribution = hooks._recall_audit_policy_attribution(event={**event, "turnId": "A"})
        assert attribution["policy_suggestion_ids"] == ["policy-A"]
        assert attribution["audit_record_id"] == first.record_id
        assert attribution["task_anchor"] == "turn:A"
    finally:
        runtime.close()


def test_session_without_immutable_task_binding_cannot_attribute_latest_audit(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        hooks = OpenClawMemoryHooks(runtime)
        event = {"session_id": "shared", "agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"}
        _audit_bound_policy(hooks, event, "policy-A")
        assert hooks._recall_audit_policy_attribution(event=event) == {}
        assert hooks._recall_audit_policy_attribution(event={**event, "task_context": {"trace_context": {"trace_id": "derived-shared"}}}) == {}
    finally:
        runtime.close()


def test_explicit_audit_reference_binds_without_session_latest_guess(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        hooks = OpenClawMemoryHooks(runtime)
        event = {"session_id": "shared", "agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"}
        first = _audit_bound_policy(hooks, {**event, "run_id": "A"}, "policy-A")
        _audit_bound_policy(hooks, {**event, "run_id": "B"}, "policy-B")
        attribution = hooks._recall_audit_policy_attribution(event={**event, "task_context": {"audit_record_id": first.record_id}})
        assert attribution["policy_suggestion_ids"] == ["policy-A"]
        assert hooks._recall_audit_policy_attribution(event={**event, "run_id": "B", "audit_record_id": first.record_id}) == {}
    finally:
        runtime.close()


def test_prompt_audit_freezes_policy_version_before_replacement(tmp_path, monkeypatch):
    import json
    from eimemory.governance.policy_rollout import policy_version
    runtime = Runtime.create(root=tmp_path)
    try:
        hooks = OpenClawMemoryHooks(runtime)
        event = {"session_id": "replace-session", "turn_id": "old-turn", "agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow", "query": "audit replacement"}
        scope = hooks._scope_from_event(event)
        old = runtime.upsert_intent_pattern({"id": "replace-policy", "pattern": "audit replacement", "execution_policy": ["old behavior"], "confidence": 0.9}, scope=scope)
        old_version = policy_version(json.loads(runtime.store.sqlite.conn.execute(
            "SELECT payload_json FROM intent_patterns WHERE id=?", (old["id"],),
        ).fetchone()[0]))
        monkeypatch.setattr(runtime, "search_policy", lambda *a, **k: {
            "ok": True, "policy_suggestions": [{"id": "replace-policy", "source": "intent_pattern"}],
        })
        prompt = hooks.before_prompt_build({**event, "task_context": {"policy_version_ids": {"replace-policy": "caller-forgery"}}})
        attribution = prompt["policy_attribution"]
        assert attribution["policy_version_ids"] == {"replace-policy": old_version}
        assert prompt["task_context"]["policy_attribution"] == attribution
        audit = runtime.store.get_by_id(attribution["audit_record_id"], scope=scope)
        assert audit.content["policy_version_ids"] == attribution["policy_version_ids"]
        runtime.upsert_intent_pattern({"id": "replace-policy", "pattern": "audit replacement", "execution_policy": ["replacement behavior"], "confidence": 0.9}, scope=scope)
        terminal = hooks.on_task_end({**event, "task_context": prompt["task_context"], "user_messages": [{"content": "audit replacement"}], "assistant_messages": [{"content": "done"}], "outcome": {"success": False}})
        assert terminal["event"]["policy_attribution"]["policy_version_ids"] == {"replace-policy": old_version}
        assert terminal["event"]["policy_version_ids"] == {"replace-policy": old_version}
        assert runtime.store.get_by_id(audit.record_id, scope=scope).content["policy_version_ids"] == {"replace-policy": old_version}
    finally:
        runtime.close()


def _policy_search_with_ids() -> dict:
    return {
        "ok": True,
        "matched_event_type": "media_playback",
        "policy_suggestions": [
            {"id": "intent-001", "source": "intent_pattern"},
            {"id": "event-outcome-007", "source": "event_outcome"},
        ],
    }


def test_openclaw_before_prompt_build_stores_policy_suggestion_audit_metadata(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    def fake_search_policy(user_phrase: str, *, scope: dict, context: dict, limit: int) -> dict:
        return _policy_search_with_ids()

    monkeypatch.setattr(runtime, "search_policy", fake_search_policy)

    result = hooks.before_prompt_build(
        {
            "session_id": "sess-audit",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "给我唱首歌",
            "task_context": {"task_type": "chat.reply"},
        }
    )
    explanation = result["memory_bundle"]["explanation"]
    assert explanation["policy_suggestion_ids"] == ["intent-001", "event-outcome-007"]
    assert explanation["policy_sources"] == ["intent_pattern", "event_outcome"]
    assert explanation["matched_event_type"] == "media_playback"

    audits = runtime.store.list_records(
        kinds=["recall_view"],
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        limit=1,
    )
    assert audits
    assert audits[0].content["policy_suggestion_ids"] == ["intent-001", "event-outcome-007"]
    assert audits[0].content["policy_sources"] == ["intent_pattern", "event_outcome"]
    assert audits[0].content["matched_event_type"] == "media_playback"
    assert audits[0].meta["policy_suggestion_ids"] == ["intent-001", "event-outcome-007"]
    assert audits[0].meta["policy_sources"] == ["intent_pattern", "event_outcome"]
    assert audits[0].meta["matched_event_type"] == "media_playback"


def test_openclaw_terminal_memory_carries_policy_attribution_from_task_context(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    result = hooks.on_task_end(
        {
            "session_id": "sess-task-context",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "task_context": {
                "policy_suggestion_ids": ["intent-001", "event-outcome-007"],
                "policy_sources": ["intent_pattern", "event_outcome"],
                "matched_event_type": "media_playback",
            },
            "user_messages": [{"content": "给我唱首歌"}],
            "assistant_messages": [{"content": "Summary: 已经发送试听片段。"}],
            "outcome": {"success": True, "notes": "completed"},
        }
    )

    assert result["event"]["policy_attribution"]["policy_suggestion_ids"] == [
        "intent-001",
        "event-outcome-007",
    ]
    assert result["event"]["policy_attribution"]["policy_sources"] == ["intent_pattern", "event_outcome"]
    assert result["event"]["policy_attribution"]["matched_event_type"] == "media_playback"
    assert result["outcome"]["policy_attribution"]["policy_suggestion_ids"] == [
        "intent-001",
        "event-outcome-007",
    ]
    assert result["outcome"]["source_trust"] == "agent_inferred"


def test_openclaw_terminal_memory_uses_recall_audit_for_policy_attribution_when_missing(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    def fake_search_policy(user_phrase: str, *, scope: dict, context: dict, limit: int) -> dict:
        return _policy_search_with_ids()

    monkeypatch.setattr(runtime, "search_policy", fake_search_policy)

    hooks.before_prompt_build(
        {
            "session_id": "sess-audit-lookup",
            "run_id": "lookup-run",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "给我唱首歌",
            "task_context": {"task_type": "chat.reply"},
        }
    )

    result = hooks.on_task_end(
        {
            "session_id": "sess-audit-lookup",
            "run_id": "lookup-run",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [{"content": "给我唱首歌"}],
            "assistant_messages": [{"content": "Summary: 已经播放歌曲。"}],
            "outcome": {"success": True, "notes": "completed"},
        }
    )

    assert result["outcome"]["policy_attribution"]["policy_suggestion_ids"] == [
        "intent-001",
        "event-outcome-007",
    ]
    assert result["outcome"]["policy_attribution"]["policy_sources"] == ["intent_pattern", "event_outcome"]
    assert result["outcome"]["policy_attribution"]["matched_event_type"] == "media_playback"


def test_openclaw_terminal_attribution_fallback_survives_more_than_ten_recent_audits(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    def fake_search_policy(user_phrase: str, *, scope: dict, context: dict, limit: int) -> dict:
        return _policy_search_with_ids()

    monkeypatch.setattr(runtime, "search_policy", fake_search_policy)

    hooks.before_prompt_build(
        {
            "session_id": "sess-audit-deep",
            "turn_id": "deep-turn",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "给我唱首歌",
            "task_context": {"task_type": "chat.reply"},
        }
    )
    for index in range(12):
        hooks.before_prompt_build(
            {
                "session_id": f"sess-audit-filler-{index}",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "user_id": "darrow",
                "query": f"filler query {index}",
                "task_context": {"task_type": "chat.reply"},
            }
        )

    result = hooks.on_task_end(
        {
            "session_id": "sess-audit-deep",
            "turn_id": "deep-turn",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [{"content": "给我唱首歌"}],
            "assistant_messages": [{"content": "Summary: 已经播放歌曲。"}],
            "outcome": {"success": True, "notes": "completed"},
        }
    )

    assert result["outcome"]["policy_attribution"]["policy_suggestion_ids"] == [
        "intent-001",
        "event-outcome-007",
    ]


def test_openclaw_duplicate_terminal_hooks_do_not_count_as_repeated_bad_outcomes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    pattern_id = "intent-duplicate-terminal"
    runtime.upsert_intent_pattern(
        {
            "id": pattern_id,
            "pattern": "duplicate terminal rollback check",
            "default_event_type": "repair",
            "interpreted_intent": "测试同一 session terminal hook 去重",
            "execution_policy": ["不要因为同一 session 的 agent_end/task_end 双写而重复计数"],
            "confidence": 0.91,
        },
        scope=scope,
    )
    event = {
        "session_id": "sess-duplicate-terminal",
        "agent_id": "main",
        "workspace_id": "repo-x",
        "user_id": "darrow",
        "user_messages": [{"content": "duplicate terminal rollback check"}],
        "assistant_messages": [{"content": "Summary: failed"}],
        "outcome": {"success": False, "notes": "same failure reported by terminal hooks"},
        "task_context": {
            "event_type": "repair",
            "policy_suggestion_ids": [pattern_id],
            "policy_sources": ["intent_pattern"],
            "matched_event_type": "repair",
        },
    }

    first = hooks.on_agent_end(event)
    second = hooks.on_task_end(event)

    assert first["outcome"].get("rollback", {}).get("triggered") is not True
    assert second["outcome"].get("rollback", {}).get("triggered") is not True
    assert pattern_id in {
        str(item.get("id") or "")
        for item in runtime.search_policy("duplicate terminal rollback check", scope=scope)["policy_suggestions"]
    }


def test_openclaw_terminal_outcome_source_trust_marks_user_explicit(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    result = hooks.on_task_end(
        {
            "session_id": "sess-user-explicit",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [
                {"content": "给我唱一首歌"},
                {"content": "不对，不是让你写歌词，我是要能听见"},
            ],
            "assistant_messages": [{"content": "我来写一段歌词。"}],
            "outcome": {"success": True, "notes": "generated lyrics"},
            "task_context": {"event_type": "media_playback"},
        }
    )

    assert result["outcome"]["source_trust"] == "user_explicit"


def test_openclaw_terminal_outcome_source_trust_marks_system_verified_on_verification(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-system-verified",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_messages": [{"content": "巡检队列"}],
            "assistant_messages": [{"content": "Summary: 队列已恢复。"}],
            "outcome": {
                "success": True,
                "notes": "completed",
                "verified": True,
                "verification": "watchdog 无新增 stuck session",
            },
            "task_context": {"event_type": "repair"},
        }
    )

    assert result["outcome"]["source_trust"] == "system_verified"


def test_openclaw_unverified_agent_terminal_outcome_is_agent_inferred_and_blocks_apply(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    hooks.on_agent_end(
        {
            "session_id": "sess-agent-inferred",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "assistant_messages": [{"content": "Summary: 已尝试修复。"}],
            "outcome": {"success": False, "notes": "agent execution failed"},
            "task_context": {"event_type": "repair"},
        }
    )

    result = hooks.on_task_end(
        {
            "session_id": "sess-agent-inferred",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [{"content": "再试一次"}],
            "assistant_messages": [{"content": "Summary: 我再次尝试修复。"}],
            "outcome": {"success": False, "notes": "再次失败"},
            "task_context": {"event_type": "repair"},
        }
    )

    assert result["outcome"]["source_trust"] == "agent_inferred"
