"""In-memory registry, freshness labels, and task-title field mapping."""
from types import SimpleNamespace

import pytest

from eimemory.ei_bridge.protocol import BridgeTarget
from eimemory.ei_bridge.registry import AgentAdapterRegistry
from eimemory.ei_bridge import eibrain_monitor, openclaw_runtime


def test_reregister_replaces_all_previous_capability_aliases():
    registry = AgentAdapterRegistry()
    old, new = object(), object()
    registry.register("agent", old, ["vision", "health"])
    registry.register("agent", new, ["health"])
    assert registry.find(BridgeTarget(capability="vision.describe")) is None
    assert registry.find(BridgeTarget(capability="health.status")) is new
    assert registry.find(BridgeTarget(agent_id="agent")) is new


def test_shared_alias_falls_back_to_remaining_registered_owner():
    registry = AgentAdapterRegistry()
    left, right = object(), object()
    registry.register("left", left, ["health"])
    registry.register("right", right, ["health"])
    assert registry.find(BridgeTarget(capability="health.status")) is right
    registry.register("right", right, [])
    assert registry.find(BridgeTarget(capability="health.status")) is left


@pytest.mark.parametrize("age", [None, -1, float("nan"), float("inf"), True])
def test_missing_or_invalid_age_is_not_live(age):
    payload = eibrain_monitor._vision_payload({"visual_diagnostics": {
        "data_status": "live", "scene_labels": ["person"], "frame_age_s": age, "state_age_s": age}})
    assert payload["observation_mode"] == "unavailable"
    assert payload["freshness"] == {"frame_age_s": None, "state_age_s": None}


@pytest.mark.parametrize("age,mode", [(0, "live"), (2, "recent"), (7, "stale")])
def test_known_age_categories_are_preserved(age, mode):
    payload = eibrain_monitor._vision_payload({"visual_diagnostics": {"scene_labels": ["person"], "frame_age_s": age}})
    assert payload["observation_mode"] == mode


def test_empty_scene_labels_fall_back_to_detections():
    assert eibrain_monitor._scene_labels({"scene_labels": ["", " "], "detections": [{"label": "cup"}]}) == ["cup"]


@pytest.mark.parametrize("params,event,expected", [
    ({"raw_text": "normalized command"}, {"query": "other"}, "normalized command"),
    ({}, {"text": "text-only command"}, "text-only command"),
])
def test_task_title_uses_parsed_or_text_only_command(monkeypatch, params, event, expected):
    captured = {}
    def create_task(**kwargs):
        captured.update(kwargs)
        return {"task_id": "synthetic"}
    monkeypatch.setattr(openclaw_runtime, "openclaw_loop", SimpleNamespace(create_task=create_task, record_heartbeat=lambda *args, **kwargs: None))
    command = SimpleNamespace(command_id="id", params=params, target=SimpleNamespace(capability="health.status"))
    result = openclaw_runtime._start_loop_task(command=command, event=event)
    assert result["task_id"] == "synthetic"
    assert captured["title"] == "Feishu command: " + expected
