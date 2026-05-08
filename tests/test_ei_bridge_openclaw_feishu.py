from __future__ import annotations

import pytest

from eimemory.ei_bridge import BridgeResult
from eimemory.ei_bridge.channels.openclaw_feishu import (
    OpenClawFeishuChannel,
    format_reply,
    parse_event,
)


def test_parse_feishu_nested_message_content_json() -> None:
    event = {
        "event": {
            "message": {
                "message_id": "om-vision",
                "chat_id": "oc-chat",
                "content": '{"text": "现在看到了什么"}',
            },
            "sender": {
                "sender_id": {
                    "user_id": "user-1",
                    "open_id": "open-1",
                }
            },
        }
    }

    command = parse_event(event)

    assert command is not None
    assert command.command_id == "om-vision"
    assert command.source.channel == "feishu"
    assert command.source.source_id == "user-1"
    assert command.source.metadata["user_id"] == "user-1"
    assert command.source.metadata["open_id"] == "open-1"
    assert command.source.metadata["conversation_id"] == "oc-chat"
    assert command.source.metadata["raw_text"] == "现在看到了什么"
    assert command.source.metadata["raw_event"] == event
    assert command.target.agent_id == "eibrain"
    assert command.target.capability == "vision.describe"
    assert command.intent == "describe_current_scene"
    assert command.params["raw_text"] == "现在看到了什么"


def test_parse_top_level_openclaw_text_shape() -> None:
    event = {
        "message_id": "msg-health",
        "text": "health",
        "open_id": "open-2",
        "conversation_id": "conv-2",
    }

    command = parse_event(event)

    assert command is not None
    assert command.command_id == "msg-health"
    assert command.source.source_id == "open-2"
    assert command.source.metadata["conversation_id"] == "conv-2"
    assert command.target.agent_id == "eibrain"
    assert command.target.capability == "health.status"
    assert command.intent == "report_health"


def test_parse_body_user_session_shape() -> None:
    event = {
        "message": {
            "body": {
                "text": "wake",
            },
            "user_id": "user-3",
            "session_id": "sess-3",
        }
    }

    command = parse_event(event)

    assert command is not None
    assert command.source.source_id == "user-3"
    assert command.source.metadata["session_id"] == "sess-3"
    assert command.target.capability == "engagement.wake"
    assert command.intent == "wake_engagement"


@pytest.mark.parametrize(
    ("text", "capability", "intent"),
    [
        ("看到什么", "vision.describe", "describe_current_scene"),
        ("看到了什么", "vision.describe", "describe_current_scene"),
        ("你看见什么", "vision.describe", "describe_current_scene"),
        ("你看见了什么", "vision.describe", "describe_current_scene"),
        ("你能看见什么", "vision.describe", "describe_current_scene"),
        ("你能看到什么", "vision.describe", "describe_current_scene"),
        ("what do you see", "vision.describe", "describe_current_scene"),
        ("系统状态", "health.status", "report_health"),
        ("健康", "health.status", "report_health"),
        ("status", "health.status", "report_health"),
        ("鸿途", "engagement.wake", "wake_engagement"),
        ("唤醒", "engagement.wake", "wake_engagement"),
        ("醒来", "engagement.wake", "wake_engagement"),
        ("休眠", "engagement.sleep", "sleep_engagement"),
        ("结束对话", "engagement.sleep", "sleep_engagement"),
        ("sleep", "engagement.sleep", "sleep_engagement"),
    ],
)
def test_parse_maps_supported_chinese_and_english_intents(
    text: str, capability: str, intent: str
) -> None:
    command = parse_event({"text": text, "user_id": "user-map"})

    assert command is not None
    assert command.target.agent_id == "eibrain"
    assert command.target.capability == capability
    assert command.intent == intent


def test_parse_unknown_message_returns_none() -> None:
    assert parse_event({"text": "随便聊聊今天的天气", "user_id": "user-4"}) is None


def test_channel_class_delegates_to_parse_event() -> None:
    command = OpenClawFeishuChannel().parse_event({"text": "sleep", "user_id": "user-5"})

    assert command is not None
    assert command.intent == "sleep_engagement"


def test_format_reply_prefers_success_summary() -> None:
    result = BridgeResult(
        ok=True,
        command_id="cmd-1",
        summary="我现在看到：桌面和机械臂都在画面里。",
        audit={"capability": "vision.describe"},
    )

    assert format_reply(result) == "我现在看到：桌面和机械臂都在画面里。"


def test_format_reply_failure_includes_error() -> None:
    result = BridgeResult(
        ok=False,
        command_id="cmd-2",
        summary="无法获取相机画面",
        error="camera_timeout",
    )

    assert format_reply(result) == "执行失败：无法获取相机画面\n错误：camera_timeout"
