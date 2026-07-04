from __future__ import annotations

import json
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_scope
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.ops import openclaw_loop

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

    loop_task = _start_loop_task(command=command, event=event)
    adapter = EIBrainAgentAdapter(transport=EIBrainMonitorTransport())
    registry = AgentAdapterRegistry()
    registry.register(agent_id=adapter.agent_id, adapter=adapter, capabilities=adapter.capabilities)
    _record_loop_dispatch(loop_task, command=command)
    result = BridgeRouter(registry).route(command)
    audit_result = EIMemoryAuditSink(lambda record: _write_audit(runtime, record)).record(command, result)
    reply = format_reply(result)
    loop_task = _finish_loop_task(loop_task, result_ok=bool(result.ok), summary=reply)
    prepend_context = _build_prepend_context(reply) if command.target.capability == "vision.describe" else ""
    return {
        "matched": True,
        "reply": reply,
        "prepend_context": prepend_context,
        "command": command.to_dict(),
        "result": result.to_dict(),
        "audit": audit_result.to_dict(),
        "loop_task": loop_task,
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


def _start_loop_task(*, command: Any, event: dict[str, Any]) -> dict[str, Any]:
    try:
        command_id = str(getattr(command, "command_id", "") or event.get("message_id") or event.get("event_id") or "")
        text = str(event.get("query") or event.get("raw_query") or event.get("content") or getattr(command, "raw_text", "") or "").strip()
        task = openclaw_loop.create_task(
            title=("Feishu command: " + text)[:160] if text else "Feishu command",
            objective=str(getattr(command.target, "capability", "") or "route Feishu command to OpenClaw agent"),
            source="feishu",
            owner="openclaw",
            risk_level="low",
            report_policy="always",
            dedupe_key=f"feishu:{command_id}" if command_id else None,
        )
        openclaw_loop.record_heartbeat(
            str(task.get("task_id") or ""),
            lease_seconds=300,
            progress="feishu command received",
            source="feishu",
        )
        return task
    except Exception as exc:
        return {"error": str(exc)}


def _record_loop_dispatch(loop_task: dict[str, Any], *, command: Any) -> None:
    task_id = str(loop_task.get("task_id") or "")
    if not task_id:
        return
    try:
        openclaw_loop.record_dispatch(
            task_id,
            dispatch_type="feishu",
            command_or_tool=str(getattr(command.target, "capability", "") or "openclaw_feishu"),
            lease_seconds=300,
            progress="bridge routing",
        )
    except Exception:
        return


def _finish_loop_task(loop_task: dict[str, Any], *, result_ok: bool, summary: str) -> dict[str, Any]:
    task_id = str(loop_task.get("task_id") or "")
    if not task_id:
        return loop_task
    try:
        openclaw_loop.record_verification(
            task_id,
            verifier="ei_bridge.openclaw_feishu",
            checks={"result_ok": result_ok, "summary": summary[:500]},
            passed=result_ok,
            failure_reason="" if result_ok else summary[:500],
            next_action="report_done" if result_ok else "repair",
        )
        return openclaw_loop.finish_task(
            task_id,
            status="done" if result_ok else "failed",
            summary=summary[:500],
        )
    except Exception as exc:
        return {**loop_task, "error": str(exc)}


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
