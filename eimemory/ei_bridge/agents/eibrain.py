from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult


type EIBrainTransportResult = dict[str, Any] | BridgeResult
type EIBrainTransport = Callable[[BridgeCommand], EIBrainTransportResult]

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
        transport: EIBrainTransport | None = None,
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
            transport_result = self.transport(command) if self.transport is not None else {}
        except Exception as exc:
            summary = _transport_error_summary(capability, exc)
            return BridgeResult(
                ok=False,
                command_id=command.command_id,
                summary=summary,
                error="transport_error",
                audit={"agent_id": self.agent_id, "capability": capability},
            )

        result = _coerce_result(transport_result, command)
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


def _coerce_result(raw_result: EIBrainTransportResult, command: BridgeCommand) -> BridgeResult:
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


def _transport_error_summary(capability: str, exc: Exception) -> str:
    if capability == "vision.describe":
        return "我这会儿还没拿到可用画面，不能把现场情况编出来。"
    return f"agent transport failed: {exc}"


def _vision_summary(payload: dict[str, Any]) -> str:
    scene = payload.get("scene") if isinstance(payload.get("scene"), dict) else {}
    objects = scene.get("objects") if isinstance(scene, dict) else None
    labels = [str(item) for item in objects] if isinstance(objects, list) else []
    description = str(payload.get("description") or "").strip()
    mode = _observation_mode(payload, labels, description)
    frame_age_s = _frame_age_seconds(payload)

    if mode == "unavailable":
        return "我这会儿还没拿到可用画面，不能把现场情况编出来。"

    details: list[str] = []
    if description and not _is_no_detection_description(description):
        details.append(description)
    if labels:
        details.append(f"识别到：{'、'.join(labels)}")
    if not details:
        return _no_detection_summary(mode, frame_age_s)

    return f"{_vision_prefix(mode, frame_age_s)}{'；'.join(details)}。"


def _observation_mode(payload: dict[str, Any], labels: list[str], description: str) -> str:
    mode = str(payload.get("observation_mode") or "").strip().lower()
    visual_status = str(payload.get("visual_status") or "").strip().lower()
    if not payload and not labels and not description:
        return "unavailable"
    if visual_status in {"unavailable", "state_unavailable", "camera_unavailable", "offline", "error"}:
        return "unavailable"

    frame_age_s = _frame_age_seconds(payload)
    if frame_age_s is not None and frame_age_s > 6.0:
        return "stale"
    if frame_age_s is not None and frame_age_s > 1.5:
        return "recent"
    if mode in {"live", "recent", "stale", "unavailable"}:
        return mode
    if visual_status in {"recent", "stale"}:
        return visual_status
    if labels or description:
        return "live"
    return "unavailable"


def _frame_age_seconds(payload: dict[str, Any]) -> float | None:
    raw = payload.get("raw")
    if isinstance(raw, dict):
        age = raw.get("frame_age_s")
        if isinstance(age, (int, float)):
            return float(age)
    freshness = payload.get("freshness")
    if isinstance(freshness, dict):
        age = freshness.get("frame_age_s")
        if isinstance(age, (int, float)):
            return float(age)
    return None


def _vision_prefix(mode: str, frame_age_s: float | None) -> str:
    if mode == "recent":
        return f"我刚拿到的是 {_age_text(frame_age_s)}前的画面："
    if mode == "stale":
        return f"我现在拿到的是 {_age_text(frame_age_s)}前的旧画面："
    return "我现在看到："


def _no_detection_summary(mode: str, frame_age_s: float | None) -> str:
    if mode == "recent":
        return f"我刚拿到的是 {_age_text(frame_age_s)}前的画面，那一帧里还没稳定识别到目标。"
    if mode == "stale":
        return f"我现在拿到的是 {_age_text(frame_age_s)}前的旧画面，那一帧里还没稳定识别到目标。"
    return "我现在看得到画面，但这帧里还没稳定识别到目标。"


def _age_text(frame_age_s: float | None) -> str:
    if frame_age_s is None:
        return "几秒"
    return f"{frame_age_s:.1f} 秒"


def _is_no_detection_description(description: str) -> bool:
    normalized = description.strip().lower()
    return normalized in {
        "",
        "当前没有稳定识别到物体",
        "no detections in current frame",
        "no recognizable face candidate in current frame",
    }


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
