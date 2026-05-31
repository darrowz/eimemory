from __future__ import annotations

from eimemory.api.runtime import Runtime


def test_active_policy_source_weights_affects_recall_ranking(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    query = "UUMit delivery quality"
    runtime.memory.ingest(
        text=f"{query} trusted delivery acceptance note.",
        memory_type="fact",
        title="Trusted delivery acceptance",
        source="trusted.delivery",
        scope=scope,
        force_capture=True,
    )
    runtime.memory.ingest(
        text=f"{query} openclaw agent execution log.",
        memory_type="conversation",
        title="OpenClaw execution log",
        source="openclaw.agent_end",
        scope=scope,
        force_capture=True,
    )
    runtime.evolution.store_rule(
        title="Prefer trusted delivery source",
        summary="Prefer trusted delivery source for delivery task recall",
        task_type="delivery.review",
        retrieval_policy={"source_weights": {"trusted.delivery": 2.5, "openclaw.agent_end": 0.1}},
        response_policy={},
        scope=scope,
        status="active",
    )

    bundle = runtime.memory.recall(
        query=query,
        scope=scope,
        task_context={"task_type": "delivery.review"},
        limit=2,
    )

    assert [item.source for item in bundle.items][:2] == ["trusted.delivery", "openclaw.agent_end"]


def test_task_context_source_weights_override_and_supplement_active_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    query = "Delivery quality policy test"
    runtime.memory.ingest(
        text=f"{query} shares ranking content with trusted source.",
        memory_type="fact",
        title="Trusted delivery acceptance",
        source="trusted.delivery",
        scope=scope,
        force_capture=True,
    )
    runtime.memory.ingest(
        text=f"{query} shares ranking content with openclaw source.",
        memory_type="fact",
        title="OpenClaw execution log",
        source="openclaw.agent_end",
        scope=scope,
        force_capture=True,
    )
    runtime.evolution.store_rule(
        title="Prefer trusted delivery source",
        summary="Prefer trusted delivery source for delivery task recall",
        task_type="delivery.review",
        retrieval_policy={"source_weights": {"trusted.delivery": 2.5, "openclaw.agent_end": 0.1}},
        response_policy={},
        scope=scope,
        status="active",
    )

    bundle = runtime.memory.recall(
        query=query,
        scope=scope,
        task_context={
            "task_type": "delivery.review",
            "source_weights": {"openclaw.agent_end": 5.0},
        },
        limit=2,
    )

    assert [item.source for item in bundle.items][:2] == ["openclaw.agent_end", "trusted.delivery"]
    recall_filters = bundle.explanation["recall_filters"]
    assert recall_filters["source_weights"]["trusted.delivery"] == 2.5
    assert recall_filters["source_weights"]["openclaw.agent_end"] == 5.0
