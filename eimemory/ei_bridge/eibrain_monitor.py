from __future__ import annotations

import json
import os
from typing import Any
from urllib import request

from .protocol import BridgeCommand

DEFAULT_MONITOR_URL = "http://127.0.0.1:18080/status.json"


class EIBrainMonitorTransport:
    def __init__(self, monitor_url: str | None = None, timeout_s: float = 3.0) -> None:
        self.monitor_url = monitor_url or os.environ.get("EIBRAIN_MONITOR_URL") or DEFAULT_MONITOR_URL
        self.timeout_s = timeout_s

    def __call__(self, command: BridgeCommand) -> dict[str, Any]:
        status = self._fetch_status()
        capability = command.target.capability or ""
        if capability == "health.status":
            return {
                "ok": True,
                "command_id": command.command_id,
                "payload": _health_payload(status),
            }
        if capability == "vision.describe":
            return {
                "ok": True,
                "command_id": command.command_id,
                "payload": _vision_payload(status),
            }
        return {
            "ok": True,
            "command_id": command.command_id,
            "summary": f"已接收 {capability}",
            "payload": {"status": "accepted", "capability": capability},
        }

    def _fetch_status(self) -> dict[str, Any]:
        with request.urlopen(self.monitor_url, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}


def _health_payload(status: dict[str, Any]) -> dict[str, Any]:
    visual = _mapping(status.get("visual_diagnostics"))
    dialogue = _mapping(status.get("dialogue_diagnostics"))
    return {
        "system_health": status.get("system_health") or "unknown",
        "visual_data_health": visual.get("data_health") or visual.get("detection_health") or "unknown",
        "engagement": {
            "state": "awake" if dialogue.get("conversation_active") else "listening",
            "phase": dialogue.get("phase") or "",
            "vision_status": visual.get("data_status") or visual.get("vision_service_status") or "",
        },
    }


def _vision_payload(status: dict[str, Any]) -> dict[str, Any]:
    visual = _mapping(status.get("visual_diagnostics"))
    labels = _scene_labels(visual)
    description = _description_from_visual(visual, labels)
    return {
        "visual_status": visual.get("data_status") or visual.get("vision_service_status") or "unknown",
        "description": description,
        "scene": {
            "objects": labels,
            "summary": visual.get("scene_summary") or "",
            "detection_count": visual.get("detection_count") or 0,
            "recognized_identity": _mapping(visual.get("recognized_identity")),
        },
        "system_health": status.get("system_health") or "unknown",
        "visual_data_health": visual.get("data_health") or "unknown",
        "raw": {
            "frame_age_s": visual.get("frame_age_s"),
            "state_age_s": visual.get("state_age_s"),
            "backend": visual.get("backend") or "",
            "detections": visual.get("detections") if isinstance(visual.get("detections"), list) else [],
        },
    }


def _scene_labels(visual: dict[str, Any]) -> list[str]:
    labels = visual.get("scene_labels")
    if isinstance(labels, list) and labels:
        return [str(item) for item in labels if str(item)]
    detections = visual.get("detections")
    if isinstance(detections, list):
        derived = []
        for item in detections:
            if isinstance(item, dict) and item.get("label"):
                derived.append(str(item["label"]))
        if derived:
            return derived
    identity = _mapping(visual.get("recognized_identity"))
    display_name = identity.get("display_name") or identity.get("actor_id")
    return [str(display_name)] if display_name else []


def _description_from_visual(visual: dict[str, Any], labels: list[str]) -> str:
    scene_summary = str(visual.get("scene_summary") or "").strip()
    identity_summary = str(visual.get("identity_summary") or "").strip()
    if labels and scene_summary and scene_summary != "no detections in current frame":
        return scene_summary
    if identity_summary and identity_summary != "no recognizable face candidate in current frame":
        return identity_summary
    if scene_summary:
        return scene_summary
    return "当前没有稳定识别到物体"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["DEFAULT_MONITOR_URL", "EIBrainMonitorTransport"]
