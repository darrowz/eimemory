from __future__ import annotations

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime


def test_memory_ingest_splits_business_and_runtime_metadata(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    stored = runtime.memory.ingest(
        text="Remember the user prefers concise status updates.",
        title="Concise status preference",
        memory_type="preference",
        source="eibrain.audio_dialogue",
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        meta={
            "hardware_node": "honxin",
            "runtime_node": "eibrain",
            "organ": "ear",
            "modality": "audio_text",
            "hardware_role": "head",
            "hardware_id": "head-v0",
            "service": "eimemory-rpc",
            "transport": "openclaw",
            "dedupe_key": "preference:concise-status",
        },
        force_capture=True,
    )

    assert stored.meta["business_meta"]["dedupe_key"] == "preference:concise-status"
    assert stored.meta["dedupe_key"] == "preference:concise-status"
    assert "hardware_node" not in stored.meta["business_meta"]
    assert "runtime_node" not in stored.meta["business_meta"]
    assert "organ" not in stored.meta["business_meta"]
    assert "modality" not in stored.meta["business_meta"]
    assert "hardware_role" not in stored.meta["business_meta"]
    assert "hardware_id" not in stored.meta["business_meta"]
    assert "service" not in stored.meta["business_meta"]
    assert "transport" not in stored.meta["business_meta"]
    assert stored.meta["runtime_meta"]["hardware_node"] == "honxin"
    assert stored.meta["runtime_meta"]["runtime_node"] == "eibrain"
    assert stored.meta["runtime_meta"]["organ"] == "ear"
    assert stored.meta["runtime_meta"]["modality"] == "audio_text"
    assert stored.meta["runtime_meta"]["hardware_role"] == "head"
    assert stored.meta["runtime_meta"]["hardware_id"] == "head-v0"
    assert stored.meta["runtime_meta"]["service"] == "eimemory-rpc"
    assert stored.meta["runtime_meta"]["transport"] == "openclaw"
    assert "hardware_node" not in stored.meta
    assert "runtime_node" not in stored.meta
    assert "organ" not in stored.meta
    assert "modality" not in stored.meta
    assert "hardware_role" not in stored.meta
    assert "hardware_id" not in stored.meta
    assert "service" not in stored.meta
    assert "transport" not in stored.meta


def test_eibrain_rpc_keeps_outcome_business_metadata_separate_from_runtime_metadata(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)

    response = bridge.handle(
        {
            "method": "memory.ingest",
            "params": {
                "text": "Observed cup on the table.",
                "title": "Visual world observation",
                "memory_type": "world_observation",
                "source": "eibrain.visual_world",
                "scope": {
                    "agent_id": "honxin",
                    "workspace_id": "honjia",
                    "user_id": "darrow",
                    "hardware_node": "honxin",
                },
                "organ": "eye",
                "modality": "vision",
                "meta": {"dedupe_key": "world:cup"},
                "outcome": {"success": True, "status": "planned"},
            },
        }
    )

    meta = response["result"]["meta"]
    assert response["ok"] is True
    assert meta["business_meta"]["dedupe_key"] == "world:cup"
    assert meta["business_meta"]["outcome"] == {"success": True, "status": "planned"}
    assert meta["runtime_meta"]["hardware_node"] == "honxin"
    assert meta["runtime_meta"]["runtime_node"] == "honxin"
    assert meta["runtime_meta"]["organ"] == "eye"
    assert meta["runtime_meta"]["modality"] == "vision"
    assert "modality" not in meta["business_meta"]


def test_runtime_metadata_modality_does_not_boost_recall_score(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    stored = runtime.memory.ingest(
        text="shared metadata layer ranking phrase",
        title="Runtime modality memory",
        memory_type="fact",
        source="eibrain.audio_dialogue",
        scope=scope,
        meta={"modality": "audio_text", "organ": "ear"},
        force_capture=True,
    )

    bundle = runtime.memory.recall(
        query="shared metadata layer ranking phrase",
        scope=scope,
        task_context={"task_type": "brain.respond", "preferred_modalities": ["audio_text"]},
        limit=1,
    )

    scoring = bundle.explanation["scoring"][0]
    assert bundle.items[0].record_id == stored.record_id
    assert scoring["record_id"] == stored.record_id
    assert scoring["modality_boost"] == 0.0
