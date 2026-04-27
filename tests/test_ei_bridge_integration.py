from __future__ import annotations

from eimemory.ei_bridge import AgentAdapterRegistry, BridgeRouter
from eimemory.ei_bridge.agents import EIBrainAgentAdapter
from eimemory.ei_bridge.audit import EIMemoryAuditSink
from eimemory.ei_bridge.channels.openclaw_feishu import format_reply, parse_event


def test_feishu_message_routes_to_eibrain_and_records_audit() -> None:
    command = parse_event({"text": "现在看到了什么", "user_id": "user-1", "message_id": "msg-1"})
    assert command is not None

    adapter = EIBrainAgentAdapter(
        transport=lambda bridge_command: {
            "ok": True,
            "command_id": bridge_command.command_id,
            "payload": {
                "visual_status": "画面稳定",
                "description": "桌面前方有人，旁边有键盘",
                "scene": {"objects": ["person", "keyboard"]},
            },
        }
    )
    registry = AgentAdapterRegistry()
    registry.register(agent_id="eibrain", adapter=adapter, capabilities=adapter.capabilities)

    result = BridgeRouter(registry).route(command)
    writes: list[dict[str, object]] = []
    audit_result = EIMemoryAuditSink(writes.append).record(command, result)

    assert result.ok is True
    assert "识别到：person、keyboard" in result.summary
    assert format_reply(result).startswith("已完成：视觉状态：画面稳定")
    assert audit_result.ok is True
    assert writes[0]["type"] == "ei_bridge.audit"
    assert writes[0]["source"]["channel"] == "feishu"
    assert writes[0]["target"]["agent_id"] == "eibrain"
