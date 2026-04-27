from __future__ import annotations

from .protocol import BridgeCommand, BridgeResult
from .registry import AgentAdapterRegistry


class BridgeRouter:
    def __init__(self, registry: AgentAdapterRegistry) -> None:
        self.registry = registry

    def route(self, command: BridgeCommand) -> BridgeResult:
        adapter = self.registry.find(command.target)
        if adapter is None:
            target = command.target.agent_id or command.target.capability or "unspecified target"
            return BridgeResult(
                ok=False,
                command_id=command.command_id,
                summary=f"Unknown bridge target: {target}",
                error="unknown_target",
                audit={
                    "agent_id": command.target.agent_id,
                    "capability": command.target.capability,
                },
            )

        return adapter.handle_command(command)


__all__ = ["BridgeRouter"]
