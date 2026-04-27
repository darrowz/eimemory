from __future__ import annotations

from eimemory.ei_bridge.audit import EIMemoryAuditSink, build_audit_record, should_persist
from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult, BridgeSource, BridgeTarget


def make_command(**overrides: object) -> BridgeCommand:
    values = {
        "command_id": "cmd-1",
        "source": BridgeSource(source_id="cli", source_type="cli", channel="terminal"),
        "target": BridgeTarget(agent_id="agent-1", capability="vision.observe"),
        "intent": "inspect_frame",
        "params": {"frame_id": "frame-1"},
        "policy": {"audit": True, "retain": "summary"},
        "created_at_ts": 1777248000.0,
    }
    values.update(overrides)
    return BridgeCommand(**values)


def test_build_audit_record_for_successful_result() -> None:
    command = make_command()
    result = BridgeResult(
        ok=True,
        command_id="cmd-1",
        summary="captured frame",
        payload={"object": "mug"},
        audit={"completed_at_ts": 1777248002.0},
    )

    record = build_audit_record(command, result, channel="hardware")

    assert record["type"] == "ei_bridge.audit"
    assert record["command_id"] == "cmd-1"
    assert record["source"]["source_id"] == "cli"
    assert record["source"]["channel"] == "hardware"
    assert record["target"]["capability"] == "vision.observe"
    assert record["intent"] == "inspect_frame"
    assert record["ok"] is True
    assert record["summary"] == "captured frame"
    assert record["error"] is None
    assert record["created_at_ts"] == 1777248000.0
    assert record["completed_at_ts"] == 1777248002.0
    assert record["payload"]["object"] == "mug"
    assert record["policy"] == {"audit": True, "retain": "summary"}


def test_build_audit_record_for_failed_result() -> None:
    command = make_command(command_id="cmd-fail")
    result = BridgeResult(
        ok=False,
        command_id="cmd-fail",
        summary="unknown bridge target",
        error="unknown_target",
    )

    record = build_audit_record(command, result)

    assert record["ok"] is False
    assert record["summary"] == "unknown bridge target"
    assert record["error"] == "unknown_target"
    assert record["payload"] == {}
    assert should_persist(command, result) is True


def test_audit_false_policy_skips_writer() -> None:
    writes: list[dict[str, object]] = []
    command = make_command(policy={"audit": False})
    result = BridgeResult(ok=True, command_id="cmd-1", payload={"object": "mug"})
    sink = EIMemoryAuditSink(writes.append)

    response = sink.record(command, result)

    assert writes == []
    assert response.ok is True
    assert response.summary == "audit skipped"
    assert should_persist(command, result) is False


def test_large_payload_is_compressed_to_summary_and_digest() -> None:
    command = make_command()
    large_blob = "x" * 5000
    result = BridgeResult(
        ok=True,
        command_id="cmd-1",
        payload={
            "image": large_blob,
            "width": 640,
            "height": 480,
            "labels": ["mug", "desk"],
            "nested": {"blob": large_blob},
        },
    )

    record = build_audit_record(command, result)

    assert record["payload_digest"]
    assert record["payload"]["keys"] == ["height", "image", "labels", "nested", "width"]
    assert record["payload"]["field_count"] == 5
    assert record["payload"]["sample"]["width"] == 640
    assert record["payload"]["sample"]["height"] == 480
    assert record["payload"]["sample"]["labels"] == ["mug", "desk"]
    assert "image" not in record["payload"]["sample"]
    assert large_blob not in str(record)


def test_sink_wraps_writer_success_value_as_bridge_result() -> None:
    writes: list[dict[str, object]] = []

    def writer(record: dict[str, object]) -> dict[str, object]:
        writes.append(record)
        return {"memory_id": "mem-1"}

    command = make_command()
    result = BridgeResult(ok=True, command_id="cmd-1", summary="captured")
    sink = EIMemoryAuditSink(writer)

    response = sink.record(command, result)

    assert len(writes) == 1
    assert response.ok is True
    assert response.command_id == "cmd-1"
    assert response.summary == "audit recorded"
    assert response.payload == {"memory_id": "mem-1"}


def test_sink_returns_stable_error_when_writer_raises() -> None:
    def writer(record: dict[str, object]) -> None:
        raise RuntimeError("memory offline")

    command = make_command()
    result = BridgeResult(ok=True, command_id="cmd-1", summary="captured")
    sink = EIMemoryAuditSink(writer)

    response = sink.record(command, result)

    assert response.ok is False
    assert response.command_id == "cmd-1"
    assert response.error == "audit_writer_error"
    assert "memory offline" in response.summary
