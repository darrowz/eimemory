from __future__ import annotations

import io
import json
import sys

from eimemory.cli.main import main as cli_main
from eimemory.ei_bridge import AgentAdapterRegistry, BridgeRouter
from eimemory.ei_bridge.agents import EIBrainAgentAdapter
from eimemory.ei_bridge.audit import EIMemoryAuditSink
from eimemory.ei_bridge.channels.openclaw_feishu import format_reply, parse_event
from eimemory.ei_bridge.protocol import BridgeCommand
from eimemory.ops import openclaw_loop as loop


def test_feishu_message_routes_to_eibrain_and_records_audit() -> None:
    command: BridgeCommand | None = parse_event({"text": "现在看到了什么", "user_id": "user-1", "message_id": "msg-1"})
    assert command is not None

    adapter = EIBrainAgentAdapter(
        transport=lambda bridge_command: {
            "ok": True,
            "command_id": bridge_command.command_id,
            "payload": {
                "visual_status": "live",
                "observation_mode": "live",
                "description": "桌面前方有人，旁边有键盘",
                "scene": {"objects": ["person", "keyboard"]},
            },
        }
    )
    registry = AgentAdapterRegistry()
    registry.register(agent_id="eibrain", adapter=adapter, capabilities=adapter.capabilities)

    result = BridgeRouter(registry).route(command)
    writes: list[dict[str, object]] = []
    audit_result = EIMemoryAuditSink(writes.append).record(command, result)

    assert result.ok is True
    assert result.summary == "我现在看到：桌面前方有人，旁边有键盘；识别到：person、keyboard。"
    assert format_reply(result).startswith("我现在看到：桌面前方有人")
    assert audit_result.ok is True
    assert writes[0]["type"] == "ei_bridge.audit"
    assert writes[0]["source"]["channel"] == "feishu"
    assert writes[0]["target"]["agent_id"] == "eibrain"


def test_cli_ei_bridge_feishu_returns_live_visual_context(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "system_health": "healthy",
                "visual_diagnostics": {
                    "data_status": "live",
                    "data_health": "healthy",
                    "scene_summary": "person and keyboard in front of camera",
                    "scene_labels": ["person", "keyboard"],
                    "detection_count": 2,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIBRAIN_MONITOR_URL", status_path.as_uri())
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"query": "现在看到了什么", "user_id": "user-1"}, ensure_ascii=False))
    try:
        assert cli_main(["ei-bridge", "feishu"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["reply"].startswith("我现在看到：person and keyboard in front of camera")
    assert "共享视觉观测" in payload["prepend_context"]
    assert "不区分飞书或现场渠道" in payload["prepend_context"]


def test_cli_ei_bridge_feishu_ignores_unmatched_text(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"query": "普通聊天", "user_id": "user-1"}, ensure_ascii=False))
    try:
        assert cli_main(["ei-bridge", "feishu"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"matched": False}


def test_cli_ei_bridge_feishu_health_does_not_inject_visual_context(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "system_health": "healthy",
                "visual_diagnostics": {
                    "data_status": "live",
                    "data_health": "healthy",
                },
                "dialogue_diagnostics": {
                    "conversation_active": False,
                    "phase": "idle",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIBRAIN_MONITOR_URL", status_path.as_uri())
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"query": "系统状态", "user_id": "user-1"}, ensure_ascii=False))
    try:
        assert cli_main(["ei-bridge", "feishu"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["reply"].startswith("已完成：系统健康：healthy")
    assert payload["prepend_context"] == ""


def test_cli_ei_bridge_feishu_creates_and_closes_loop_task(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "loop"))
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "system_health": "healthy",
                "visual_diagnostics": {
                    "data_status": "live",
                    "data_health": "healthy",
                    "scene_summary": "person and keyboard in front of camera",
                    "scene_labels": ["person", "keyboard"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIBRAIN_MONITOR_URL", status_path.as_uri())
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"query": "现在看到了什么", "user_id": "user-1", "message_id": "msg-loop"}, ensure_ascii=False))
    try:
        assert cli_main(["ei-bridge", "feishu"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["loop_task"]["status"] == "done"
    tasks = loop.load_tasks()
    assert len(tasks) == 1
    assert tasks[0]["source"] == "feishu"
    assert tasks[0]["dedupe_key"] == "feishu:msg-loop"
    assert loop.read_jsonl("actions.jsonl")[0]["action_type"] == "dispatch"
    assert loop.read_jsonl("verifications.jsonl")[0]["passed"] is True
    assert len(loop.read_jsonl("reports.jsonl")) == 1
