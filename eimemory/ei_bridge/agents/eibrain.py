from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult

DEFAULT_CAPABILITIES = (
    "vision.describe",
    "health.status",
    "engagement.wake",
    "engagement.sleep",
)


class EIBrainAgentAdapter:
    def __init__(
        self,
        agent_id: str = "eibrain",
        capabilities: tuple[str, ...] | list[str] = DEFAULT_CAPABILITIES,
        transport: Callable[[BridgeCommand], dict[str, Any] | BridgeResult] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.capabilities = tuple(capabilities)
        self.transport = transport

    def handle_command(self, command: BridgeCommand) -> BridgeResult:
        capability = command.target.capability or ""
        if self.transport is None and capability in {"engagement.wake", "engagement.sleep"}:
            return BridgeResult(
                ok=True,
                command_id=command.command_id,
                summary=f"已接受 {capability}，等待部署服务执行",
                payload={"status": "accepted", "planned": True},
                audit={"agent_id": self.agent_id, "capability": capability},
            )

        try:
            raw_result = self.transport(command) if self.transport is not None else {}
        except Exception as exc:
            return BridgeResult(
                ok=False,
                command_id=command.command_id,
                summary=f"agent transport failed: {exc}",
                error="transport_error",
                audit={"agent_id": self.agent_id, "capability": capability},
            )

        result = _coerce_result(raw_result, command)
        summary = _summary_for_capability(capability, result.payload)
        if summary:
            return BridgeResult(
                ok=result.ok,
                command_id=result.command_id,
                summary=summary,
                payload=result.payload,
                error=result.error,
                audit={**result.audit, "agent_id": self.agent_id, "capability": capability},
            )

        return BridgeResult(
            ok=result.ok,
            command_id=result.command_id,
            summary=result.summary,
            payload=result.payload,
            error=result.error,
            audit={**result.audit, "agent_id": self.agent_id, "capability": capability},
        )


def _coerce_result(raw_result: dict[str, Any] | BridgeResult, command: BridgeCommand) -> BridgeResult:
    if isinstance(raw_result, BridgeResult):
        return raw_result

    data = dict(raw_result)
    data.setdefault("ok", True)
    data.setdefault("command_id", command.command_id)
    return BridgeResult.from_dict(data)


def _summary_for_capability(capability: str, payload: dict[str, Any]) -> str:
    if capability == "vision.describe":
        return _vision_summary(payload)
    if capability == "health.status":
        return _health_summary(payload)
    if capability in {"engagement.wake", "engagement.sleep"}:
        return f"已下发 {capability}"
    return ""


def _vision_summary(payload: dict[str, Any]) -> str:
    scene = payload.get("scene") if isinstance(payload.get("scene"), dict) else {}
    objects = scene.get("objects") if isinstance(scene, dict) else None
    parts: list[str] = []

    if payload.get("visual_status"):
        parts.append(f"视觉状态：{payload['visual_status']}")
    if payload.get("description"):
        parts.append(f"画面描述：{payload['description']}")
    if isinstance(objects, list) and objects:
        parts.append(f"识别到：{'、'.join(str(item) for item in objects)}")

    return "；".join(parts) if parts else "暂时没有可用视觉状态"


def _health_summary(payload: dict[str, Any]) -> str:
    engagement = payload.get("engagement")
    if isinstance(engagement, dict):
        engagement_state = engagement.get("state") or engagement.get("status") or "未知"
    else:
        engagement_state = engagement or "未知"

    return "；".join(
        [
            f"系统健康：{payload.get('system_health') or '未知'}",
            f"视觉数据：{payload.get('visual_data_health') or '未知'}",
            f"参与状态：{engagement_state}",
        ]
    )


__all__ = ["EIBrainAgentAdapter"]
