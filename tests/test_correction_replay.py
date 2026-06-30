from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "hongtu", "workspace_id": "correction-loop", "user_id": "darrow"}


def test_user_correction_becomes_lesson_replay_and_graph_edges(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.record_user_correction_replay(
        {
            "text": "\u4e0d\u8981\u8bf4\u505a\u4e0d\u5230\uff0c\u8981\u8865\u80fd\u529b\u89e3\u51b3",
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

    report = runtime.record_user_correction_replay({"text": "\u597d\u7684"}, scope=SCOPE, persist=True)

    assert report["ok"] is True
    assert report["skipped"] is True
    assert report["skipped_reason"] == "trivial_message"
    assert runtime.store.list_records(kinds=["replay_result", "reflection", "rule"], scope=SCOPE, limit=10) == []


def test_ground_truth_rules_are_returned_as_pre_answer_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    replay = runtime.record_user_correction_replay(
        {
            "text": "\u56de\u7b54\u7248\u672c\u3001\u90e8\u7f72\u3001\u72b6\u6001\u95ee\u9898\u524d\u5fc5\u987b\u5148\u67e5\u8fd0\u884c\u6001\u8bc1\u636e",
            "context": "assistant answered version status from memory",
            "target_capability": "evidence.query_first",
            "expected_behavior": "Query git/runtime/deploy evidence before answering status questions.",
        },
        scope=SCOPE,
        persist=True,
    )

    gate = runtime.build_ground_truth_pre_answer_gate(
        query="\u73b0\u5728 eimemory \u90e8\u7f72\u7248\u672c\u662f\u591a\u5c11\uff1f",
        scope=SCOPE,
        persist=True,
    )

    assert gate["ok"] is True
    assert gate["gate_required"] is True
    assert gate["verdict"] == "pass"
    assert gate["matched_rule_count"] == 1
    assert gate["rules"][0]["rule_id"] == replay["ground_truth_rule_id"]
    assert gate["rules"][0]["priority"] == "T0"
    assert gate["rules"][0]["must_use"] is True
    assert gate["replay_gate"]["expected_behavior"] == "Query git/runtime/deploy evidence before answering status questions."
    assert gate["record_id"]
