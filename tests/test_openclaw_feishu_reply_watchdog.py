from __future__ import annotations

import json
from pathlib import Path

import eimemory.ops.openclaw_feishu_reply_watchdog as watchdog
from eimemory.ops.openclaw_feishu_reply_watchdog import (
    _delivery_idempotency_key,
    _parse_command_result,
    scan_once as _scan_once,
    send_payload,
)


def scan_once(**kwargs):
    kwargs.setdefault("find_existing", lambda _payload: {"status": "not_found"})
    return _scan_once(**kwargs)


def test_delivery_idempotency_key_is_stable_and_platform_bounded() -> None:
    key = _delivery_idempotency_key("om_x100b6a9c4b9634a0dda21cbae010358", "final")

    assert key == _delivery_idempotency_key("om_x100b6a9c4b9634a0dda21cbae010358", "final")
    assert key != _delivery_idempotency_key("om_x100b6a9c4b9634a0dda21cbae010358", "status")
    assert len(key) <= 50


def test_parse_command_result_accepts_nested_feishu_fast_receipt() -> None:
    result = _parse_command_result('{"ok":true,"data":{"message_id":"om_nested"}}')

    assert result["ok"] is True
    assert result["messageId"] == "om_nested"


def test_parse_command_result_rejects_success_without_receipt() -> None:
    result = _parse_command_result('{"ok":true,"data":{}}')

    assert result["ok"] is False
    assert "messageId" in result["error"]


def _write_state(path: Path, entry: dict) -> None:
    path.write_text(
        json.dumps(
            {"schema_version": "openclaw_reply_delivery.v1", "entries": {entry["inbound_message_id"]: entry}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_watchdog_retries_overdue_answer_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_in_1",
            "session_key": "agent:main:feishu:direct:ou_test",
            "conversation_id": "oc_test",
            "sender_id": "ou_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "最终答复",
            "delivery_message_id": "",
        },
    )
    calls: list[dict] = []

    def send(payload: dict) -> dict:
        calls.append(payload)
        return {"ok": True, "messageId": "om_retry_1"}

    first = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=70_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )
    second = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=80_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )

    assert first == {"checked": 1, "retried": 1, "failed": 0}
    assert second == {"checked": 1, "retried": 0, "failed": 0}
    assert calls == [
        {
            "conversation_id": "oc_test",
            "sender_id": "ou_test",
            "text": "最终答复",
            "idempotency_key": _delivery_idempotency_key("om_in_1", "final"),
            "inbound_message_id": "om_in_1",
            "received_at_ms": 1_000,
        }
    ]
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
    assert attempts["entries"]["om_in_1"]["message_id"] == "om_retry_1"


def test_watchdog_sends_when_conversation_id_is_canonical_user_target(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_user_target",
            "session_key": "agent:main:feishu:direct:ou_test",
            "conversation_id": "user:ou_test",
            "sender_id": "ou_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "可靠补发",
        },
    )
    calls: list[dict] = []

    result = _scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=10_000,
        find_existing=watchdog.find_existing_reply,
        send=lambda payload: calls.append(payload) or {"ok": True, "messageId": "om_sent"},
    )

    assert result == {"checked": 1, "retried": 1, "failed": 0}
    assert calls[0]["inbound_message_id"] == "om_user_target"


def test_sender_replies_to_inbound_with_provider_idempotency(monkeypatch) -> None:
    captured: dict = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return type("Result", (), {"returncode": 0, "stdout": '{"ok":true,"data":{"message_id":"om_sent"}}', "stderr": ""})()

    monkeypatch.setattr(watchdog.subprocess, "run", run)
    result = send_payload(
        {
            "inbound_message_id": "om_inbound",
            "text": "可靠答复",
            "idempotency_key": _delivery_idempotency_key("om_inbound", "final"),
        }
    )

    assert result["messageId"] == "om_sent"
    assert captured["command"] == [
        "lark-cli", "im", "+messages-reply",
        "--message-id", "om_inbound",
        "--markdown", "可靠答复",
        "--idempotency-key", _delivery_idempotency_key("om_inbound", "final"),
    ]


def test_reply_query_error_defers_send(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_query_error",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "最终答复",
        },
    )
    sends: list[dict] = []

    result = _scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=10_000,
        find_existing=lambda _payload: {"status": "error", "error": "rate limited"},
        send=lambda payload: sends.append(payload) or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result["failed"] == 1
    assert sends == []


def test_existing_parent_reply_closes_ambiguous_send_without_duplicate(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_ambiguous",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "相同答复",
        },
    )
    sends: list[dict] = []

    result = _scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=10_000,
        find_existing=lambda _payload: {"status": "found", "messageId": "om_existing"},
        send=lambda payload: sends.append(payload) or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result["retried"] == 1
    assert sends == []
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"]
    assert attempts["om_ambiguous"]["message_id"] == "om_existing"


def test_watchdog_attempt_state_write_failure_is_fail_open(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_disk_full",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "已发送",
        },
    )
    monkeypatch.setattr(watchdog, "_write_json_atomic", lambda *_args: (_ for _ in ()).throw(OSError("full")))

    result = scan_once(
        state_path=state_path,
        attempts_path=tmp_path / "attempts.json",
        now_ms=70_000,
        delivery_timeout_ms=60_000,
        send=lambda _payload: {"ok": True, "messageId": "om_sent"},
    )

    assert result == {"checked": 1, "retried": 1, "failed": 0}


def test_watchdog_skips_delivered_reply(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_in_2",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "delivered",
            "final_text": "已送达",
            "delivery_message_id": "om_out_2",
        },
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=500_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=lambda payload: calls.append(payload) or {"ok": True},
    )

    assert result == {"checked": 1, "retried": 0, "failed": 0}
    assert calls == []


def test_watchdog_reports_stalled_turn_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_in_3",
            "conversation_id": "oc_test",
            "sender_id": "ou_test",
            "received_at_ms": 1_000,
            "status": "pending",
            "final_text": "",
            "delivery_message_id": "",
        },
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=302_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=lambda payload: calls.append(payload) or {"ok": True, "messageId": "om_notice"},
    )

    assert result == {"checked": 1, "retried": 1, "failed": 0}
    assert "处理链路异常" in calls[0]["text"]
    assert calls[0]["idempotency_key"] == _delivery_idempotency_key("om_in_3", "status")


def test_watchdog_skips_intentionally_silent_pending_turn(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_silent",
            "received_at_ms": 1_000,
            "status": "pending",
            "suppress_stalled_notice": True,
            "final_text": "",
        },
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=500_000,
        send=lambda payload: calls.append(payload) or {"ok": True, "messageId": "unexpected"},
    )

    assert result == {"checked": 1, "retried": 0, "failed": 0}
    assert calls == []


def test_stalled_notice_does_not_consume_final_delivery_slot(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    entry = {
        "inbound_message_id": "om_late_final",
        "conversation_id": "oc_test",
        "received_at_ms": 1_000,
        "status": "pending",
        "final_text": "",
    }
    _write_state(state_path, entry)
    calls: list[dict] = []
    send = lambda payload: calls.append(payload) or {"ok": True, "messageId": f"om_{len(calls)}"}

    scan_once(state_path=state_path, attempts_path=attempts_path, now_ms=302_000, send=send)
    entry.update(status="answered", final_text="最终结果", agent_end_at_ms=303_000)
    _write_state(state_path, entry)
    scan_once(state_path=state_path, attempts_path=attempts_path, now_ms=309_000, send=send)

    assert [call["idempotency_key"] for call in calls] == [
        _delivery_idempotency_key("om_late_final", "status"),
        _delivery_idempotency_key("om_late_final", "final"),
    ]
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"]
    assert attempts["status:om_late_final"]["ok"] is True
    assert attempts["om_late_final"]["ok"] is True


def test_watchdog_retries_failed_fallback_with_same_idempotency_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_in_4",
            "conversation_id": "oc_test",
            "sender_id": "ou_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "最终答复",
            "delivery_message_id": "",
        },
    )
    calls: list[dict] = []

    def send(payload: dict) -> dict:
        calls.append(payload)
        if len(calls) == 1:
            return {"ok": False, "error": "temporary platform failure"}
        return {"ok": True, "messageId": "om_retry_4"}

    first = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=70_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )
    too_soon = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=80_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )
    recovered = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=131_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )

    assert first["failed"] == 1
    assert too_soon["retried"] == 0
    assert recovered["retried"] == 1
    assert [item["idempotency_key"] for item in calls] == [
        _delivery_idempotency_key("om_in_4", "final"),
        _delivery_idempotency_key("om_in_4", "final"),
    ]
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
    assert attempts["entries"]["om_in_4"]["retry_count"] == 2


def test_watchdog_enters_persistent_backoff_after_rapid_failures(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_bounded",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "最终答复",
        },
    )
    calls: list[dict] = []
    send = lambda payload: calls.append(payload) or {"ok": False, "error": "down"}

    for now_ms in (70_000, 76_000, 82_000, 88_000, 383_000):
        scan_once(
            state_path=state_path,
            attempts_path=attempts_path,
            now_ms=now_ms,
            delivery_timeout_ms=5_000,
            backoff_ms=300_000,
            send=send,
        )

    assert len(calls) == 4
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
    assert attempts["entries"]["om_bounded"]["retry_count"] == 4
    assert attempts["entries"]["om_bounded"]["retry_mode"] == "backoff"
