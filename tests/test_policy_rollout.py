from __future__ import annotations

from eimemory.api.runtime import Runtime


def _intent_pattern_ids(result: dict, *, source: str = "intent_pattern") -> set[str]:
    return {
        str(item.get("id") or "")
        for item in result.get("policy_suggestions") or []
        if str(item.get("source") or "") == source
    }


def test_search_policy_default_only_returns_active_patterns(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    query = "policy rollout status filter alpha"
    active_id = "pr-status-active"
    shadow_id = "pr-status-shadow"
    rolled_back_id = "pr-status-rolled-back"
    candidate_id = "pr-status-candidate"

    runtime.upsert_intent_pattern(
        {
            "id": active_id,
            "pattern": query,
            "default_event_type": "media_playback",
            "interpreted_intent": "测试 active pattern 回显",
            "status": "active",
            "confidence": 0.96,
        },
        scope=scope,
    )
    runtime.upsert_intent_pattern(
        {
            "id": shadow_id,
            "pattern": query,
            "default_event_type": "media_playback",
            "interpreted_intent": "测试 shadow pattern 回显",
            "status": "shadow",
            "confidence": 0.95,
        },
        scope=scope,
    )
    runtime.upsert_intent_pattern(
        {
            "id": rolled_back_id,
            "pattern": query,
            "default_event_type": "media_playback",
            "interpreted_intent": "测试 rolled_back pattern 回显",
            "status": "rolled_back",
            "confidence": 0.94,
        },
        scope=scope,
    )
    runtime.upsert_intent_pattern(
        {
            "id": candidate_id,
            "pattern": query,
            "default_event_type": "media_playback",
            "interpreted_intent": "测试 candidate pattern 回显",
            "status": "candidate",
            "confidence": 0.93,
        },
        scope=scope,
    )

    default_result = runtime.search_policy(query, scope=scope)
    default_pattern_ids = _intent_pattern_ids(default_result)
    assert default_pattern_ids == {active_id}

    shadow_result = runtime.search_policy(query, scope=scope, context={"include_shadow": True})
    shadow_pattern_ids = _intent_pattern_ids(shadow_result)
    assert shadow_pattern_ids == {active_id, shadow_id}


def test_auto_promotion_records_ledger_entry(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    pattern_id = "pr-auto-promotion-ok"
    opportunity_id = "opp-auto-01"
    result = runtime.upsert_intent_pattern(
        {
            "id": pattern_id,
            "pattern": "auto promotion policy sample",
            "default_event_type": "media_playback",
            "interpreted_intent": "演示自动推广写 ledger",
            "confidence": 0.89,
            "source_opportunity_id": opportunity_id,
            "source_opportunity": {
                "opportunity_id": opportunity_id,
                "opportunity_type": "intent_policy",
                "source": "auto-evolution",
            },
            "trust_report": {"ok": True},
            "replay_report": {"ok": True},
        },
        scope=scope,
    )

    assert result["_promotion_budget_decision"] == "ok"
    assert result.get("status") == "active"

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="promotion")
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["action_type"] == "promotion"
    assert entry["is_auto"] is True
    assert entry["applied_pattern_id"] == pattern_id
    assert entry["promotion_id"] == result["_promotion_id"]
    assert entry["budget_decision"] == "ok"
    assert entry["source_opportunity_id"] == opportunity_id
    assert entry["source_opportunity"]["opportunity_id"] == opportunity_id


def test_bad_attributed_outcome_rolls_back_active_pattern_and_hides_it(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    pattern_id = "pr-bad-attributed"
    runtime.upsert_intent_pattern(
        {
            "id": pattern_id,
            "pattern": "attributed bad outcome rollback",
            "default_event_type": "media_playback",
            "interpreted_intent": "测试 attributed bad outcome 回滚",
            "confidence": 0.93,
        },
        scope=scope,
    )

    event = runtime.record_event(
        {
            "id": "evt-bad-outcome-attributed",
            "source": "manual",
            "user_phrase": "播放这首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音频给用户",
            "goal": "成功播放",
            "confidence": 0.88,
        },
        scope=scope,
    )
    outcome = runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "策略建议没命中用户意图",
            "correction_from_user": "不是这个意思，先确认 playback 能力",
            "policy_update": "播放请求先确认播放路径和音量",
            "policy_attribution": {
                "policy_suggestion_ids": [pattern_id],
                "policy_sources": ["intent_pattern"],
                "matched_event_type": "media_playback",
            },
        },
        scope=scope,
    )

    rollback_report = outcome.get("rollback") or {}
    assert rollback_report.get("triggered") is True
    assert pattern_id in rollback_report.get("rolled_back_pattern_ids", [])

    runtime_view = runtime.search_policy("attributed bad outcome rollback", scope=scope)
    visible_pattern_ids = _intent_pattern_ids(runtime_view)
    assert pattern_id not in visible_pattern_ids

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rollback")
    rollback_entry = [entry for entry in ledger if entry["rollback_policy_id"] == pattern_id]
    assert rollback_entry
    assert rollback_entry[0]["is_auto"] is True
    assert rollback_entry[0]["budget_decision"] == "ok"
    assert rollback_entry[0]["details"]["rollback"]["pattern_id"] == pattern_id


def test_manual_rollback_hides_pattern_and_writes_ledger(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    pattern_id = "pr-manual-rollback"
    runtime.upsert_intent_pattern(
        {
            "id": pattern_id,
            "pattern": "manual rollback policy sample",
            "default_event_type": "media_playback",
            "interpreted_intent": "测试手动回滚",
            "confidence": 0.9,
        },
        scope=scope,
    )

    rollback = runtime.rollback_intent_pattern(pattern_id, scope=scope, reason="回归测试手动回滚")

    assert rollback.get("ok") is True
    assert rollback.get("status") == "rolled_back"
    assert rollback.get("pattern_id") == pattern_id

    runtime_view = runtime.search_policy("manual rollback policy sample", scope=scope)
    assert pattern_id not in _intent_pattern_ids(runtime_view)

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rollback")
    manual_entry = [entry for entry in ledger if entry["rollback_policy_id"] == pattern_id]
    assert manual_entry
    assert manual_entry[0]["is_auto"] is False
    assert manual_entry[0]["details"]["rollback"]["pattern_id"] == pattern_id
    assert manual_entry[0]["reason"] == "回归测试手动回滚"


def test_promotion_daily_budget_exhaustion_keeps_auto_pattern_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    for index in range(1, 4):
        promotion = runtime.upsert_intent_pattern(
            {
                "id": f"pr-budget-ok-{index}",
                "pattern": f"daily budget ok {index}",
                "default_event_type": "media_playback",
                "interpreted_intent": "测试预算未满时自动升级",
                "confidence": 0.9,
                "source_opportunity_id": f"opp-budget-{index}",
                "source_opportunity": {
                    "opportunity_id": f"opp-budget-{index}",
                    "opportunity_type": "intent_policy",
                },
            },
            scope=scope,
        )
        assert promotion["_promotion_budget_decision"] == "ok"
        assert promotion.get("status") == "active"

    exhausted = runtime.upsert_intent_pattern(
        {
            "id": "pr-budget-exhausted",
            "pattern": "daily budget exhausted promotion",
            "default_event_type": "media_playback",
            "interpreted_intent": "测试预算耗尽后仍为 candidate",
            "confidence": 0.88,
            "source_opportunity_id": "opp-budget-4",
            "source_opportunity": {
                "opportunity_id": "opp-budget-4",
                "opportunity_type": "intent_policy",
            },
        },
        scope=scope,
    )

    assert exhausted["_promotion_budget_decision"] == "budget_exhausted"
    assert exhausted.get("status") == "candidate"

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="promotion")
    exhausted_entry = [entry for entry in ledger if entry["source_opportunity_id"] == "opp-budget-4"]
    assert len(exhausted_entry) == 1
    assert exhausted_entry[0]["applied_pattern_id"] == ""
    assert exhausted_entry[0]["budget_decision"] == "budget_exhausted"

    runtime_view = runtime.search_policy("daily budget exhausted promotion", scope=scope)
    assert "pr-budget-exhausted" not in _intent_pattern_ids(runtime_view)
