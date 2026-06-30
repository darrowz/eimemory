from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "hongtu", "workspace_id": "correction-loop", "user_id": "darrow"}


def test_user_correction_becomes_lesson_replay_and_graph_edges(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.record_user_correction_replay(
        {
            "text": "不要说做不到，要补能力解决",
            "context": "assistant refused instead of building missing capability",
            "target_capability": "proactive.judgment",
            "expected_behavior": "When a capability is missing, create a concrete plan, replay, and gated implementation path.",
        },
        scope=SCOPE,
        persist=True,
    )

    assert report["ok"] is True
    assert report["lesson_record_id"]
    assert report["replay_record_id"]
    assert report["ground_truth_rule_id"]
    assert report["replay"]["verdict"] == "pass"
    assert {"trigger", "expected_behavior", "gate", "behavior_check"}.issubset(report["replay_case"])

    edges = runtime.store.list_memory_edges(scope=SCOPE, record_ids=[report["lesson_record_id"]], limit=20)
    relations = {edge.meta.get("relation") for edge in edges}
    assert {"CORRECTED_FAILURE", "DECIDED_BEHAVIOR", "VALIDATED_BY_REPLAY", "ENFORCED_BY_GROUND_TRUTH"}.issubset(relations)

    rule = runtime.store.get_by_id(report["ground_truth_rule_id"], scope=SCOPE)
    assert rule is not None
    assert rule.kind == "rule"
    assert rule.status == "active"
    assert rule.meta["report_type"] == "ground_truth_behavior_rule"
    assert rule.meta["priority"] == "T0"
    assert rule.meta["must_use"] is True
    assert rule.content["pre_action_protocol"] == [
        "inventory_ground_truth_rules",
        "match_current_task",
        "apply_matching_rule_or_record_gap",
        "verify_behavior_with_replay_gate",
    ]


def test_trivial_user_correction_is_skipped_to_avoid_memory_pollution(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.record_user_correction_replay({"text": "好的"}, scope=SCOPE, persist=True)

    assert report["ok"] is True
    assert report["skipped"] is True
    assert report["skipped_reason"] == "trivial_message"
    assert runtime.store.list_records(kinds=["replay_result", "reflection", "rule"], scope=SCOPE, limit=10) == []
