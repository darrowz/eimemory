from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.governance.promotion_watch import record_promotion_observation
from eimemory.governance.sandbox_lab import create_sandbox_experiment


PASSING_EVAL = {"verdict": "pass", "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8, "evidence": 1.0}}


def test_policy_candidate_starts_in_shadow_observe_not_active(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-shadow-start")

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["post_promotion_status"] == "shadow_observe"
    assert runtime.store.get_by_id(candidate_id).status == "shadow_observe"

    pattern = _intent_pattern(runtime, "watch-shadow-start")
    assert pattern["status"] == "shadow"
    assert pattern["post_promotion_watch"]["status"] == "shadow_observe"
    assert pattern["post_promotion_watch"]["required_observations"] == 3

    default_policy = runtime.search_policy("post promotion hit sample", scope=scope)
    assert "watch-shadow-start" not in _intent_pattern_ids(default_policy)

    shadow_policy = runtime.search_policy("post promotion hit sample", scope=scope, context={"include_shadow": True})
    assert "watch-shadow-start" in _intent_pattern_ids(shadow_policy)


def test_shadow_observe_activates_after_three_hit_improvement_observations(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-activate")
    promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    for index in range(3):
        event = runtime.record_event(
            {
                "id": f"evt-hit-{index}",
                "source": "test",
                "user_phrase": "post promotion hit sample",
                "event_type": "tool_routing",
                "interpreted_intent": "Use the observed policy",
                "goal": "Improve policy routing",
                "confidence": 0.9,
            },
            scope=scope,
        )
        outcome = runtime.record_outcome(
            event["id"],
            {
                "outcome": "good",
                "reason": "shadow policy improved the task",
                "policy_attribution": {"policy_suggestion_ids": ["watch-activate"]},
            },
            scope=scope,
        )
        report = outcome["post_promotion_watch"][0]

    assert report["status"] == "active"
    assert report["activated"] is True
    assert runtime.store.get_by_id(candidate_id).status == "promoted"
    assert _intent_pattern(runtime, "watch-activate")["status"] == "active"

    default_policy = runtime.search_policy("post promotion hit sample", scope=scope)
    assert "watch-activate" in _intent_pattern_ids(default_policy)

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="shadow_observe", limit=10)
    assert ledger[0]["details"]["decision"] == "active"
    assert ledger[0]["details"]["observed_count"] == 3


def test_shadow_observe_quarantines_after_three_real_tasks_without_hits(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-quarantine")
    promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    for index in range(3):
        report = record_promotion_observation(
            runtime,
            pattern_id="watch-quarantine",
            scope=scope,
            event_id=f"evt-miss-{index}",
            hit=False,
            improved=False,
            outcome="uncertain",
        )

    assert report["status"] == "quarantined"
    assert report["quarantined"] is True
    assert runtime.store.get_by_id(candidate_id).status == "quarantined"
    assert _intent_pattern(runtime, "watch-quarantine")["status"] == "quarantined"

    shadow_policy = runtime.search_policy("post promotion hit sample", scope=scope, context={"include_shadow": True})
    assert "watch-quarantine" not in _intent_pattern_ids(shadow_policy)


def test_shadow_observe_bad_outcome_rolls_back_pattern(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-rollback")
    promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    for index in range(3):
        report = record_promotion_observation(
            runtime,
            pattern_id="watch-rollback",
            scope=scope,
            event_id=f"evt-bad-shadow-{index}",
            hit=True,
            improved=False,
            outcome="bad",
            reason="shadow policy caused a worse task outcome",
        )

    assert report["status"] == "rolled_back"
    assert report["rolled_back"] is True
    assert runtime.store.get_by_id(candidate_id).status == "rolled_back"
    assert _intent_pattern(runtime, "watch-rollback")["status"] == "rolled_back"

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rollback", limit=10)
    assert ledger[0]["rollback_policy_id"] == "watch-rollback"


def test_shadow_observe_waits_for_three_events_and_uses_failure_rate_threshold(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-failure-rate")
    promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    first = record_promotion_observation(
        runtime,
        pattern_id="watch-failure-rate",
        scope=scope,
        event_id="evt-good-1",
        hit=True,
        improved=True,
        outcome="good",
    )
    second = record_promotion_observation(
        runtime,
        pattern_id="watch-failure-rate",
        scope=scope,
        event_id="evt-bad-1",
        hit=True,
        improved=False,
        outcome="bad",
        reason="one bad canary event",
    )
    third = record_promotion_observation(
        runtime,
        pattern_id="watch-failure-rate",
        scope=scope,
        event_id="evt-good-2",
        hit=True,
        improved=True,
        outcome="good",
    )

    assert first["status"] == "shadow_observe"
    assert second["status"] == "shadow_observe"
    assert third["status"] in {"rolled_back", "quarantined"}
    assert third["watch"]["observed_count"] == 3
    assert third["watch"]["failure_rate"] >= 0.2
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rolled_back", limit=10)
    assert ledger[0]["details"]["observed_count"] == 3
    assert ledger[0]["details"]["failure_rate"] >= 0.2


def test_shadow_observe_promotes_active_with_low_failure_rate_ledger(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-active-rate")
    promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    for index in range(3):
        report = record_promotion_observation(
            runtime,
            pattern_id="watch-active-rate",
            scope=scope,
            event_id=f"evt-active-{index}",
            hit=True,
            improved=True,
            outcome="good",
        )

    assert report["status"] == "active"
    assert report["watch"]["failure_rate"] <= 0.05
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="promoted_active", limit=10)
    assert ledger[0]["details"]["observed_count"] == 3
    assert ledger[0]["details"]["failure_rate"] <= 0.05


def test_learning_dashboard_lists_current_promotion_status(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    candidate_id = _policy_candidate(runtime, scope=scope, pattern_id="watch-dashboard")
    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=_passing_eval(), health={"ok": True})

    dashboard = runtime.build_learning_dashboard(scope=scope, persist=False)

    statuses = dashboard["promotion_statuses"]
    assert any(
        item["candidate_id"] == candidate_id
        and item["promotion_request_id"] == result["promotion_request_id"]
        and item["status"] == "shadow_observe"
        for item in statuses
    )


def _policy_candidate(runtime: Runtime, *, scope: dict, pattern_id: str) -> str:
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id=f"goal-{pattern_id}",
        research_note_id=f"note-{pattern_id}",
        candidate_kind="prompt_policy",
        candidate_patch={
            "id": pattern_id,
            "pattern": "post promotion hit sample",
            "default_event_type": "tool_routing",
            "interpreted_intent": "Use the post-promotion policy only after shadow observation.",
            "execution_policy": ["Prefer the shadow-observed route when it has real hit evidence."],
            "success_criteria": "Three real task observations hit without regression.",
        },
    )
    return distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id=experiment_id,
        eval_result=_passing_eval(),
        promotion_target="prompt_policy",
        summary="Post-promotion policy candidate",
        target_capability="tool.routing",
    )


def _passing_eval() -> dict:
    return {**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}


def _l2_gate_bundle() -> dict:
    return {
        "evidence": [{"tier": "T0", "ref": "evt_1", "summary": "User correction verified"}],
        "rollback": {"available": True, "executable": True},
        "canary": {"passed": True, "blast_radius": "single_scope"},
        "timeout_seconds": 300,
        "audit": {"enabled": True},
        "prompt_shadow_eval": {"passed": True},
        "prompt_injection_check": {"passed": True},
    }


def _intent_pattern(runtime: Runtime, pattern_id: str) -> dict:
    row = runtime.store.sqlite.conn.execute("SELECT payload_json FROM intent_patterns WHERE id = ?", (pattern_id,)).fetchone()
    assert row is not None
    return json.loads(str(row["payload_json"]))


def _intent_pattern_ids(result: dict) -> set[str]:
    return {
        str(item.get("id") or "")
        for item in result.get("policy_suggestions") or []
        if str(item.get("source") or "") == "intent_pattern"
    }
