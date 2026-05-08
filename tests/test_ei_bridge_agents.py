from __future__ import annotations

from eimemory.ei_bridge.agents import EIBrainAgentAdapter, GenericHTTPAgentAdapter
from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult, BridgeSource, BridgeTarget


def _command(command_id: str, capability: str, intent: str = "test") -> BridgeCommand:
    return BridgeCommand(
        command_id=command_id,
        source=BridgeSource(source_id="feishu", source_type="chat", channel="dm"),
        target=BridgeTarget(agent_id="eibrain", capability=capability),
        intent=intent,
    )


def test_eibrain_vision_describe_returns_readable_summary() -> None:
    adapter = EIBrainAgentAdapter(
        transport=lambda command: {
            "ok": True,
            "command_id": command.command_id,
            "payload": {
                "scene": {"objects": ["杯子", "键盘"]},
                "visual_status": "live",
                "observation_mode": "live",
                "description": "桌面上有工作物品",
            },
        }
    )

    result = adapter.handle_command(_command("cmd-vision", "vision.describe"))

    assert result.ok is True
    assert result.command_id == "cmd-vision"
    assert result.summary == "我现在看到：桌面上有工作物品；识别到：杯子、键盘。"
    assert result.payload["scene"]["objects"] == ["杯子", "键盘"]


def test_eibrain_vision_describe_marks_recent_snapshot_with_age() -> None:
    adapter = EIBrainAgentAdapter(
        transport=lambda command: {
            "ok": True,
            "command_id": command.command_id,
            "payload": {
                "scene": {"objects": ["person"]},
                "visual_status": "live",
                "observation_mode": "recent",
                "description": "画面里有人站在摄像头前",
                "raw": {"frame_age_s": 2.6},
            },
        }
    )

    result = adapter.handle_command(_command("cmd-recent-vision", "vision.describe"))

    assert result.ok is True
    assert result.summary == "我刚拿到的是 2.6 秒前的画面：画面里有人站在摄像头前；识别到：person。"


def test_eibrain_vision_describe_downgrades_explicit_live_when_frame_is_old() -> None:
    adapter = EIBrainAgentAdapter(
        transport=lambda command: {
            "ok": True,
            "command_id": command.command_id,
            "payload": {
                "scene": {"objects": ["person"]},
                "visual_status": "live",
                "observation_mode": "live",
                "description": "画面里有人站在摄像头前",
                "raw": {"frame_age_s": 7.2},
            },
        }
    )

    result = adapter.handle_command(_command("cmd-stale-vision", "vision.describe"))

    assert result.ok is True
    assert result.summary == "我现在拿到的是 7.2 秒前的旧画面：画面里有人站在摄像头前；识别到：person。"


def test_eibrain_vision_describe_handles_missing_visual_data() -> None:
    adapter = EIBrainAgentAdapter(transport=lambda command: {"ok": True, "command_id": command.command_id})

    result = adapter.handle_command(_command("cmd-empty-vision", "vision.describe"))

    assert result.ok is True
    assert result.summary == "我这会儿还没拿到可用画面，不能把现场情况编出来。"


def test_eibrain_health_status_returns_stable_summary() -> None:
    adapter = EIBrainAgentAdapter(
        transport=lambda command: BridgeResult(
            ok=True,
            command_id=command.command_id,
            payload={
                "system_health": "正常",
                "visual_data_health": "最近 10 秒有更新",
                "engagement": {"state": "awake", "last_change": "manual"},
            },
        )
    )

    result = adapter.handle_command(_command("cmd-health", "health.status"))

    assert result.ok is True
    assert result.summary == "系统健康：正常；视觉数据：最近 10 秒有更新；参与状态：awake"


def test_eibrain_wake_and_sleep_are_accepted_without_transport() -> None:
    adapter = EIBrainAgentAdapter()

    wake = adapter.handle_command(_command("cmd-wake", "engagement.wake", intent="wake"))
    sleep = adapter.handle_command(_command("cmd-sleep", "engagement.sleep", intent="sleep"))

    assert wake.ok is True
    assert wake.summary == "已接受 engagement.wake，等待部署服务执行"
    assert wake.payload["status"] == "accepted"
    assert sleep.ok is True
    assert sleep.summary == "已接受 engagement.sleep，等待部署服务执行"
    assert sleep.payload["status"] == "accepted"


def test_eibrain_transport_error_returns_failed_result() -> None:
    def failing_transport(command: BridgeCommand) -> dict[str, object]:
        raise RuntimeError("boom")

    adapter = EIBrainAgentAdapter(transport=failing_transport)

    result = adapter.handle_command(_command("cmd-error", "health.status"))

    assert result.ok is False
    assert result.command_id == "cmd-error"
    assert result.error == "transport_error"
    assert "boom" in result.summary


def test_generic_http_adapter_serializes_command_for_request_callable() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_request(endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint, payload))
        return {
            "ok": True,
            "command_id": payload["command_id"],
            "summary": "forwarded",
            "payload": {"accepted": True},
        }

    adapter = GenericHTTPAgentAdapter(endpoint="https://agent.local/bridge", request=fake_request)
    command = _command("cmd-http", "vision.describe")

    result = adapter.handle_command(command)

    assert result.ok is True
    assert result.summary == "forwarded"
    assert calls == [("https://agent.local/bridge", command.to_dict())]
