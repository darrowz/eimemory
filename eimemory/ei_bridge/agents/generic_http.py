from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult


class GenericHTTPAgentAdapter:
    def __init__(
        self,
        endpoint: str,
        request: Callable[[str, dict[str, Any]], dict[str, Any] | BridgeResult],
    ) -> None:
        self.endpoint = endpoint
        self.request = request

    def handle_command(self, command: BridgeCommand) -> BridgeResult:
        try:
            raw_result = self.request(self.endpoint, command.to_dict())
        except Exception as exc:
            return BridgeResult(
                ok=False,
                command_id=command.command_id,
                summary=f"agent request failed: {exc}",
                error="request_error",
                audit={"endpoint": self.endpoint},
            )

        if isinstance(raw_result, BridgeResult):
            return raw_result

        data = dict(raw_result)
        data.setdefault("ok", True)
        data.setdefault("command_id", command.command_id)
        result = BridgeResult.from_dict(data)
        return BridgeResult(
            ok=result.ok,
            command_id=result.command_id,
            summary=result.summary,
            payload=result.payload,
            error=result.error,
            audit={**result.audit, "endpoint": self.endpoint},
        )


__all__ = ["GenericHTTPAgentAdapter"]
