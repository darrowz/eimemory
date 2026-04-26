from __future__ import annotations

from eimemory.api.runtime import Runtime


def test_recall_filters_allowed_and_blocked_sources(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.memory.ingest(
        text="Hongtu should use this eibrain audio memory for filter tests.",
        title="EIBrain audio memory",
        memory_type="conversation",
        source="eibrain.audio_dialogue",
        scope=scope,
        meta={"organ": "ear", "modality": "audio_text"},
        force_capture=True,
    )
    runtime.memory.ingest(
        text="Hongtu should not use this generic knowledge claim for filter tests.",
        title="Generic knowledge claim",
        memory_type="fact",
        source="eimemory.knowledge.claims",
        scope=scope,
        meta={"organ": "knowledge", "modality": "text"},
        force_capture=True,
    )

    bundle = runtime.memory.recall(
        query="Hongtu filter tests",
        scope=scope,
        task_context={
            "task_type": "brain.respond",
            "allowed_sources": ["eibrain.audio_dialogue"],
            "blocked_sources": ["eimemory.knowledge.claims"],
            "allowed_memory_types": ["conversation"],
            "organs": ["ear"],
            "preferred_modalities": ["audio_text"],
        },
        limit=8,
    )

    assert [item.source for item in bundle.items] == ["eibrain.audio_dialogue"]
    assert bundle.explanation["recall_filters"]["blocked_sources"] == ["eimemory.knowledge.claims"]


def test_recall_source_weights_affect_ranking_without_filtering(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.memory.ingest(
        text="shared ranking phrase from generic source",
        title="Generic source",
        memory_type="fact",
        source="generic.source",
        scope=scope,
        force_capture=True,
    )
    runtime.memory.ingest(
        text="shared ranking phrase from preferred source",
        title="Preferred source",
        memory_type="fact",
        source="eibrain.policy",
        scope=scope,
        force_capture=True,
    )

    bundle = runtime.memory.recall(
        query="shared ranking phrase",
        scope=scope,
        task_context={"task_type": "brain.respond", "source_weights": {"eibrain.policy": 2.0}},
        limit=2,
    )

    assert [item.source for item in bundle.items] == ["eibrain.policy", "generic.source"]
