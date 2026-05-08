from __future__ import annotations

import json
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_scope
from eimemory.models.records import RecordEnvelope, ScopeRef

from .agents import EIBrainAgentAdapter
from .audit import EIMemoryAuditSink, build_audit_record
from .channels.openclaw_feishu import format_reply, parse_event
from .eibrain_monitor import EIBrainMonitorTransport
from .registry import AgentAdapterRegistry
from .router import BridgeRouter


def handle_openclaw_feishu_event(event: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    command = parse_event(_event_for_channel_parser(event))
    if command is None:
        return {"matched": False}

    adapter = EIBrainAgentAdapter(transport=EIBrainMonitorTransport())
    registry = AgentAdapterRegistry()
    registry.register(agent_id=adapter.agent_id, adapter=adapter, capabilities=adapter.capabilities)
    result = BridgeRouter(registry).route(command)
    audit_result = EIMemoryAuditSink(lambda record: _write_audit(runtime, record)).record(command, result)
    reply = format_reply(result)
    prepend_context = _build_prepend_context(reply) if command.target.capability == "vision.describe" else ""
    return {
        "matched": True,
        "reply": reply,
        "prepend_context": prepend_context,
        "command": command.to_dict(),
        "result": result.to_dict(),
        "audit": audit_result.to_dict(),
    }


def _event_for_channel_parser(event: dict[str, Any]) -> dict[str, Any]:
    text = str(event.get("query") or event.get("raw_query") or event.get("content") or "").strip()
    normalized = dict(event)
    if text and "text" not in normalized:
        normalized["text"] = text
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        normalized.setdefault("open_id", metadata.get("senderId") or metadata.get("sender_id"))
        normalized.setdefault("message_id", metadata.get("messageId") or metadata.get("message_id"))
    normalized.setdefault("user_id", event.get("user_id") or event.get("userId") or event.get("senderId") or event.get("sender_id"))
    normalized.setdefault(
        "conversation_id",
        event.get("conversation_id") or event.get("conversationId") or event.get("session_id") or event.get("sessionId"),
    )
    return normalized


def _build_prepend_context(reply: str) -> str:
    return (
        "共享视觉观测（这是鸿途当前通过 honjia 视觉链路拿到的同一主体观测，不区分飞书或现场渠道）：\n"
        f"{reply}\n\n"
        "请把这段观测当作鸿途当前掌握的视觉状态来回答；如果这里说明画面过期、视觉休眠或暂时无数据，就如实说明，不要补全现场细节。"
    )


def _write_audit(runtime: Runtime, record: dict[str, Any]) -> dict[str, Any]:
    scope = hongtu_scope({"agent_id": "hongtu", "workspace_id": "embodied"})
    stored = runtime.store.append(
        RecordEnvelope.create(
            kind="recall_view",
            title="ei-bridge OpenClaw command audit",
            summary=str(record.get("summary") or record.get("intent") or "ei bridge audit"),
            content={
                "text": json.dumps(record, ensure_ascii=False, sort_keys=True),
                "memory_type": "audit",
                "record": record,
            },
            scope=ScopeRef.from_dict(scope),
            tags=["ei_bridge", "audit", "openclaw", "feishu"],
            source="ei_bridge.openclaw_feishu",
            meta={
                "memory_type": "audit",
                "communication_channel": "feishu",
                "official_channel": True,
                "bridge_type": "ei_bridge",
                "command_id": record.get("command_id", ""),
            },
        )
    )
    return {"record_id": stored.record_id}


__all__ = ["handle_openclaw_feishu_event"]
