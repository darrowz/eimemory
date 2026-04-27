from __future__ import annotations

from typing import Any

from .protocol import BridgeTarget


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._by_agent_id: dict[str, Any] = {}
        self._by_capability: dict[str, Any] = {}

    def register(self, agent_id: str, adapter: Any, capabilities: list[str] | tuple[str, ...] = ()) -> None:
        self._by_agent_id[agent_id] = adapter
        for capability in capabilities:
            self._by_capability[capability] = adapter

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
