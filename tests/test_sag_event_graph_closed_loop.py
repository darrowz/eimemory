from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.closed_loop import post_experience_hook


def test_post_experience_hook_projects_sag_event_memory_into_full_loop(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.generate_learning_thoughts = lambda **kwargs: {"ok": True, "thoughts": []}  # type: ignore[method-assign]
    scope = {"agent_id": "eibrain", "workspace_id": "ops", "user_id": "darrow"}

    outcome = runtime.record_outcome_trace(
        {
            "trace_id": "sag-loop-trace-1",
            "task_type": "ops.health",
            "input_summary": "Verify eimemory 8091 health after release 1.6.7",
            "expected_tools": ["curl"],
            "selected_tools": [],
            "actions": [{"type": "reply_without_health_check"}],
            "outcome": {"status": "bad"},
            "cost": 0.2,
        },
        scope=scope,
    )

    report = post_experience_hook(runtime, outcome, scope)

    event_graph = report["event_graph"]
    assert event_graph["ok"] is True
    assert event_graph["projection"] == "sag_event_memory"
    assert event_graph["event_record_id"]
    assert event_graph["edge_count"] >= 2
    assert {"entity", "causal"} <= set(event_graph["edge_counts"])

    event_record = runtime.store.get_by_id(event_graph["event_record_id"], scope=scope)
    assert event_record is not None
    assert event_record.kind == "memory"
    assert event_record.meta["projection_type"] == "event_memory"
    assert event_record.meta["memory_type"] == "event_trace"
    assert event_record.meta["source_record_id"] == outcome["record_id"]
    assert "eimemory" in event_record.content["entities"]
    assert "8091" in event_record.content["entities"]

    recall = runtime.memory.recall(
        query="why did eimemory 8091 health fail after release 1.6.7",
        scope=scope,
        task_context={"task_type": "ops.health"},
        limit=5,
    )

    assert event_graph["event_record_id"] in [item.record_id for item in recall.items]
    assert recall.explanation["graph_route"]["event_graph"] is True
    assert recall.explanation["event_graph"]["selected_event_count"] >= 1
    event_refs = [
        ref for ref in recall.explanation["evidence_refs"]
        if ref["record_id"] == event_graph["event_record_id"]
    ]
    assert event_refs
    assert event_refs[0]["event_id"] == event_graph["event_id"]
    assert event_refs[0]["outcome_id"] == outcome["record_id"]

    policy = runtime.search_policy("Verify eimemory 8091 health", scope=scope)
    assert any(item["source"] == "event_outcome" for item in policy["policy_suggestions"])

    assert report["rl"]["ok"] is True
    assert runtime.store.list_records(kinds=["rl_transition"], scope=scope, limit=1)
    assert runtime.store.list_records(kinds=["rl_policy_value"], scope=scope, limit=1)
