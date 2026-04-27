from __future__ import annotations

from eimemory.ei_bridge import (
    AgentAdapterRegistry,
    BridgeCommand,
    BridgeEvent,
    BridgeResult,
    BridgeRouter,
    BridgeSource,
    BridgeTarget,
)


class EchoAgent:
    def __init__(self) -> None:
        self.commands: list[BridgeCommand] = []

    def handle_command(self, command: BridgeCommand) -> BridgeResult:
        self.commands.append(command)
        return BridgeResult(
            ok=True,
            command_id=command.command_id,
            summary=f"handled {command.intent}",
            payload={"agent_id": command.target.agent_id},
            audit={"source": command.source.source_id},
        )


def test_bridge_command_round_trips_through_dict() -> None:
    command = BridgeCommand(
        command_id="cmd-1",
        source=BridgeSource(source_id="feishu", source_type="chat", channel="dm"),
        target=BridgeTarget(agent_id="hongtu", capability="memory.recall"),
        intent="recall_context",
        params={"query": "concise replies"},
        policy={"notify": False},
        created_at_ts=1777248000.0,
    )

    restored = BridgeCommand.from_dict(command.to_dict())

    assert restored == command
    assert restored.to_dict()["source"]["source_id"] == "feishu"
    assert restored.to_dict()["target"]["capability"] == "memory.recall"


def test_router_routes_by_target_agent_id() -> None:
    agent = EchoAgent()
    registry = AgentAdapterRegistry()
    registry.register(agent_id="hongtu", adapter=agent, capabilities=["memory"])
    router = BridgeRouter(registry)

    result = router.route(
        BridgeCommand(
            command_id="cmd-agent",
            source=BridgeSource(source_id="cli", source_type="cli"),
            target=BridgeTarget(agent_id="hongtu", capability="memory.recall"),
            intent="recall_context",
        )
    )

    assert result.ok is True
    assert result.command_id == "cmd-agent"
    assert result.payload == {"agent_id": "hongtu"}
    assert agent.commands[0].command_id == "cmd-agent"


def test_router_falls_back_to_capability_prefix() -> None:
    agent = EchoAgent()
    registry = AgentAdapterRegistry()
    registry.register(agent_id="sensor-agent", adapter=agent, capabilities=["vision.observe"])
    router = BridgeRouter(registry)

    result = router.route(
        BridgeCommand(
            command_id="cmd-capability",
            source=BridgeSource(source_id="web", source_type="http"),
            target=BridgeTarget(capability="vision.observe.frame"),
            intent="observe_world",
        )
    )

    assert result.ok is True
    assert result.summary == "handled observe_world"
    assert agent.commands[0].target.capability == "vision.observe.frame"


def test_router_returns_error_for_unknown_target() -> None:
    router = BridgeRouter(AgentAdapterRegistry())
    command = BridgeCommand(
        command_id="cmd-missing",
        source=BridgeSource(source_id="web", source_type="http"),
        target=BridgeTarget(agent_id="missing", capability="memory.recall"),
        intent="recall_context",
    )

    result = router.route(command)

    assert result.ok is False
    assert result.command_id == "cmd-missing"
    assert result.error == "unknown_target"
    assert "missing" in result.summary


def test_bridge_event_round_trips_through_dict() -> None:
    event = BridgeEvent(
        event_id="evt-1",
        agent_id="hongtu",
        event_type="memory.updated",
        summary="Remembered preference",
        importance=0.8,
        should_notify=True,
        memory_ref={"record_id": "mem-1"},
        payload={"title": "Preference"},
    )

    restored = BridgeEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.to_dict()["should_notify"] is True
    assert restored.to_dict()["memory_ref"] == {"record_id": "mem-1"}
