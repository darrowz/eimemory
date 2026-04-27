from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from eimemory.ei_bridge.protocol import (
    BridgeCommand,
    BridgeResult,
    BridgeSource,
    BridgeTarget,
)


_INTENT_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("现在看到了什么", "看到什么", "你看见什么", "what do you see"),
        "vision.describe",
        "describe_current_scene",
    ),
    (
        ("系统状态", "健康", "health", "status"),
        "health.status",
        "report_health",
    ),
    (
        ("鸿途", "唤醒", "醒来", "wake"),
        "engagement.wake",
        "wake_engagement",
    ),
    (
        ("结束对话", "休眠", "sleep"),
        "engagement.sleep",
        "sleep_engagement",
    ),
)


def parse_event(event: dict[str, Any]) -> BridgeCommand | None:
    raw_text = _extract_text(event)
    if not raw_text:
        return None

    intent_match = _match_intent(raw_text)
    if intent_match is None:
        return None

    capability, intent = intent_match
    user_id = _find_first(event, "user_id")
    open_id = _find_first(event, "open_id")
    source_id = _string_or_empty(user_id or open_id or "unknown-feishu-user")
    conversation_id = _find_first(event, "conversation_id") or _find_first(event, "chat_id")
    session_id = _find_first(event, "session_id")

    metadata = {
        "user_id": _string_or_empty(user_id),
        "open_id": _string_or_empty(open_id),
        "conversation_id": _string_or_empty(conversation_id),
        "session_id": _string_or_empty(session_id),
        "raw_text": raw_text,
        "raw_event": event,
    }

    return BridgeCommand(
        command_id=_extract_command_id(event),
        source=BridgeSource(
            source_id=source_id,
            source_type="chat",
            channel="feishu",
            metadata=metadata,
        ),
        target=BridgeTarget(agent_id="eibrain", capability=capability),
        intent=intent,
        params={
            "raw_text": raw_text,
            "raw_event": event,
        },
    )


def format_reply(result: BridgeResult) -> str:
    if result.ok:
        summary = result.summary.strip() or _payload_summary(result.payload)
        return f"已完成：{summary}" if summary else "已完成。"

    summary = result.summary.strip() or "请求未完成"
    if result.error:
        return f"执行失败：{summary}\n错误：{result.error}"
    return f"执行失败：{summary}"


class OpenClawFeishuChannel:
    def parse_event(self, event: dict[str, Any]) -> BridgeCommand | None:
        return parse_event(event)

    def format_reply(self, result: BridgeResult) -> str:
        return format_reply(result)


def _match_intent(text: str) -> tuple[str, str] | None:
    normalized = _normalize_text(text)
    for phrases, capability, intent in _INTENT_RULES:
        if any(_normalize_text(phrase) in normalized for phrase in phrases):
            return capability, intent
    return None


def _extract_text(event: dict[str, Any]) -> str:
    for key in ("text", "raw_text"):
        value = _find_first(event, key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    content = _find_first(event, "content")
    if isinstance(content, str):
        parsed = _parse_json_object(content)
        if parsed is not None:
            text = _extract_text(parsed)
            if text:
                return text
        if content.strip():
            return content.strip()

    body = _find_first(event, "body")
    if isinstance(body, dict):
        return _extract_text(body)
    if isinstance(body, str):
        parsed = _parse_json_object(body)
        if parsed is not None:
            return _extract_text(parsed)
        return body.strip()

    return ""


def _find_first(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_first(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, key)
            if found is not None:
                return found
    return None


def _extract_command_id(event: dict[str, Any]) -> str:
    for key in ("message_id", "event_id", "id"):
        value = _find_first(event, key)
        if value:
            return str(value)
    return f"feishu-{uuid4().hex}"


def _payload_summary(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    return summary.strip() if isinstance(summary, str) else ""


def _parse_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _string_or_empty(value: Any) -> str:
    return "" if value is None else str(value)
