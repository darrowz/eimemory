from __future__ import annotations

from typing import Any

from .protocol import BridgeTarget


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._by_agent_id: dict[str, Any] = {}
        self._by_capability: dict[str, Any] = {}
        self._capabilities_by_agent: dict[str, tuple[str, ...]] = {}

    def register(self, agent_id: str, adapter: Any, capabilities: list[str] | tuple[str, ...] = ()) -> None:
        # Registration replaces an agent's complete advertisement. Rebuild in
        # registration order so removed aliases vanish and prior owners of a
        # shared alias become available again when the latest owner drops it.
        self._by_agent_id.pop(agent_id, None)
        self._by_agent_id[agent_id] = adapter
        self._capabilities_by_agent[agent_id] = tuple(capabilities)
        self._by_capability.clear()
        for owner, registered_adapter in self._by_agent_id.items():
            for capability in self._capabilities_by_agent[owner]:
                self._by_capability[capability] = registered_adapter

    def find(self, target: BridgeTarget) -> Any | None:
        if target.agent_id and target.agent_id in self._by_agent_id:
            return self._by_agent_id[target.agent_id]

        if not target.capability:
            return None

        matches = [
            (prefix, adapter)
            for prefix, adapter in self._by_capability.items()
            if target.capability == prefix or target.capability.startswith(f"{prefix}.")
        ]
        if not matches:
            return None

        return max(matches, key=lambda item: len(item[0]))[1]


__all__ = ["AgentAdapterRegistry"]
