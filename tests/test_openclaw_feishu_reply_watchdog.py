from __future__ import annotations

import json
from contextlib import nullcontext
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


def test_parse_command_result_accepts_platform_receipt_without_ok_flag() -> None:
    result = _parse_command_result(
        '{"channel":"feishu","messageId":"om_platform_receipt"}'
    )

    assert result["ok"] is True
    assert result["messageId"] == "om_platform_receipt"


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


def test_watchdog_skips_pending_without_feishu_reply_correlation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "internal-event",
            "session_key": "agent:main:feishu:direct:unknown",
            "conversation_id": "",
            "sender_id": "",
            "received_at_ms": 1_000,
            "status": "pending",
            "final_text": "",
        },
    )
    find_calls: list[dict] = []
    send_calls: list[dict] = []

    result = _scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=500_000,
        find_existing=lambda payload: (
            find_calls.append(payload)
            or {"status": "error", "error": "missing reply correlation fields"}
        ),
        send=lambda payload: (
            send_calls.append(payload)
            or {"ok": True, "messageId": "unexpected"}
        ),
    )

    assert result == {"checked": 1, "retried": 0, "failed": 0}
    assert find_calls == []
    assert send_calls == []


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


def test_sender_replies_through_configured_openclaw_feishu_channel(monkeypatch) -> None:
    captured: dict = {}
    command_env = {"OPENCLAW_GATEWAY_TOKEN": "test-gateway-token"}

    def run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return type("Result", (), {"returncode": 0, "stdout": '{"ok":true,"data":{"message_id":"om_sent"}}', "stderr": ""})()

    monkeypatch.setattr(watchdog.subprocess, "run", run)
    monkeypatch.setattr(
        watchdog,
        "_openclaw_command_environment",
        lambda: nullcontext(command_env),
    )
    result = send_payload(
        {
            "inbound_message_id": "om_inbound",
            "conversation_id": "user:ou_test",
            "sender_id": "ou_test",
            "text": "可靠答复",
            "idempotency_key": _delivery_idempotency_key("om_inbound", "final"),
        }
    )

    assert result["messageId"] == "om_sent"
    assert captured["command"] == [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--account", "default",
        "--target", "ou_test",
        "--reply-to", "om_inbound",
        "--message", "可靠答复",
        "--json",
    ]
    assert captured["env"] is command_env


def test_openclaw_command_env_inherits_auth_from_active_gateway(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)
    monkeypatch.setattr(watchdog, "_gateway_main_pid", lambda: 4242)
    monkeypatch.setattr(
        watchdog,
        "_read_process_environment",
        lambda _pid: {
            "OPENCLAW_GATEWAY_TOKEN": "inherited-token",
            "UNRELATED": "ignored",
        },
    )

    command_env = watchdog._openclaw_command_env()

    assert command_env["OPENCLAW_GATEWAY_TOKEN"] == "inherited-token"
    assert "UNRELATED" not in command_env


def test_openclaw_command_environment_strips_gateway_token_ref(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "auth": {
                        "mode": "token",
                        "token": {
                            "source": "env",
                            "provider": "openclaw",
                            "id": "OPENCLAW_GATEWAY_TOKEN",
                        },
                    }
                },
                "messages": {"visibleReplies": "message_tool"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(
        watchdog,
        "_openclaw_command_env",
        lambda: {
            "OPENCLAW_CONFIG_PATH": str(config_path),
            "OPENCLAW_GATEWAY_TOKEN": "inherited-token",
        },
    )

    with watchdog._openclaw_command_environment() as command_env:
        temporary_path = Path(command_env["OPENCLAW_CONFIG_PATH"])
        temporary_config = json.loads(temporary_path.read_text(encoding="utf-8"))
        assert temporary_path != config_path
        assert "token" not in temporary_config["gateway"]["auth"]
        assert temporary_config["messages"]["visibleReplies"] == "message_tool"

    assert not temporary_path.exists()


def test_watchdog_unit_can_read_active_gateway_auth_environment() -> None:
    unit_text = Path(
        "deploy/systemd/openclaw-feishu-reply-watchdog.service"
    ).read_text(encoding="utf-8")

    assert "NoNewPrivileges=true" in unit_text
    assert "PrivateTmp=true" not in unit_text


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


def test_watchdog_attempt_state_write_failure_blocks_external_send(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        watchdog,
        "prepare_delivery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")),
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=tmp_path / "attempts.json",
        now_ms=70_000,
        delivery_timeout_ms=60_000,
        send=lambda payload: calls.append(payload)
        or {"ok": True, "messageId": "om_sent"},
    )

    assert result["retried"] == 0
    assert result["failed"] == 1
    assert result["persistence_failed"] == 1
    assert calls == []


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


def test_watchdog_never_resends_after_ambiguous_external_failure(tmp_path: Path) -> None:
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
    reconciled = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=131_000,
        delivery_timeout_ms=60_000,
        stalled_timeout_ms=300_000,
        send=send,
    )

    assert first["failed"] == 1
    assert too_soon["retried"] == 0
    assert reconciled["retried"] == 0
    assert [item["idempotency_key"] for item in calls] == [
        _delivery_idempotency_key("om_in_4", "final"),
    ]
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
    assert attempts["entries"]["om_in_4"]["state"] == "delivery_uncertain"
    assert attempts["entries"]["om_in_4"]["attempt_count"] == 1


def test_watchdog_does_not_resend_persisted_sending_intent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_crashed",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "final_ready",
            "final_text": "final answer",
        },
    )
    attempts_path.write_text(
        json.dumps(
            {
                "schema_version": "feishu_delivery_state.v2",
                "entries": {
                    "om_crashed": {
                        "state": "sending",
                        "delivery_kind": "final",
                        "intent_at_ms": 3_000,
                        "attempt_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=70_000,
        delivery_timeout_ms=5_000,
        send=lambda payload: calls.append(payload)
        or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result["retried"] == 0
    assert calls == []
    attempt = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"][
        "om_crashed"
    ]
    assert attempt["state"] == "delivery_uncertain"


def test_status_only_turn_escalates_after_sla_without_resume_reference(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_abandoned",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "status": "pending",
            "final_text": "",
        },
    )
    attempts_path.write_text(
        json.dumps(
            {
                "schema_version": "feishu_delivery_state.v2",
                "entries": {
                    "status:om_abandoned": {
                        "state": "status_notified",
                        "delivery_kind": "status",
                        "platform_message_id": "om_notice",
                        "platform_accepted_at_ms": 302_000,
                        "attempt_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    calls: list[dict] = []
    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=10_861_000,
        send=lambda payload: calls.append(payload) or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result["escalated"] == 1
    assert calls == []
    attempt = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"][
        "status:om_abandoned"
    ]
    assert attempt["state"] == "escalated"
    assert attempt["escalation_reason"] == "pending_without_resume_reference"


def test_status_only_turn_with_stale_resume_reference_eventually_escalates(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_stale_resume",
            "conversation_id": "oc_test",
            "received_at_ms": 1_000,
            "status": "pending",
            "resume_reference": "task-that-never-finished",
            "final_text": "",
        },
    )
    attempts_path.write_text(
        json.dumps(
            {
                "schema_version": "feishu_delivery_state.v2",
                "entries": {
                    "status:om_stale_resume": {
                        "state": "status_notified",
                        "delivery_kind": "status",
                        "message_id": "om_notice",
                        "attempted_at_ms": 302_000,
                        "attempt_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=10_861_000,
        send=lambda payload: calls.append(payload) or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result["escalated"] == 1
    assert calls == []
    attempt = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"]["status:om_stale_resume"]
    assert attempt["state"] == "escalated"
    assert attempt["escalation_reason"] == "pending_after_resume_reference"


def test_watchdog_normalizes_legacy_platform_receipt_without_resending(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    _write_state(
        state_path,
        {
            "inbound_message_id": "om_legacy_receipt",
            "conversation_id": "user:ou_test",
            "sender_id": "ou_test",
            "received_at_ms": 1_000,
            "agent_end_at_ms": 2_000,
            "status": "answered",
            "final_text": "already delivered",
        },
    )
    attempts_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery_attempts.v1",
                "entries": {
                    "om_legacy_receipt": {
                        "attempted_at_ms": 3_000,
                        "ok": False,
                        "message_id": "om_platform_receipt",
                        "error": "",
                        "retry_count": 4,
                        "retry_mode": "backoff",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []

    result = scan_once(
        state_path=state_path,
        attempts_path=attempts_path,
        now_ms=500_000,
        send=lambda payload: calls.append(payload)
        or {"ok": True, "messageId": "om_duplicate"},
    )

    assert result == {"checked": 1, "retried": 0, "failed": 0}
    assert calls == []
    attempt = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"][
        "om_legacy_receipt"
    ]
    assert attempt["ok"] is True
    assert attempt["retry_mode"] == "complete"


def test_watchdog_reconciles_only_after_first_external_failure(tmp_path: Path) -> None:
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

    assert len(calls) == 1
    attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
    assert attempts["entries"]["om_bounded"]["attempt_count"] == 1
    assert attempts["entries"]["om_bounded"]["state"] == "delivery_uncertain"
    assert attempts["entries"]["om_bounded"]["retry_mode"] == "reconcile_only"


def test_watchdog_prunes_terminal_attempt_history_but_protects_active_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    attempts_path = tmp_path / "attempts.json"
    state_path.write_text(
        json.dumps(
            {
                "entries": {
                    "om_active": {
                        "inbound_message_id": "om_active",
                        "status": "pending",
                        "received_at_ms": 999_000,
                        "suppress_stalled_notice": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    attempts = {
        f"om_old_{index}": {
            "state": "platform_accepted",
            "message_id": f"receipt_{index}",
            "attempted_at_ms": index,
        }
        for index in range(8)
    }
    attempts["om_active"] = {
        "state": "platform_accepted",
        "message_id": "receipt_active",
        "attempted_at_ms": 0,
    }
    attempts_path.write_text(
        json.dumps({"schema_version": "feishu_delivery_state.v2", "entries": attempts}),
        encoding="utf-8",
    )
    monkeypatch.setattr(watchdog, "MAX_ATTEMPT_ENTRIES", 3)

    result = scan_once(state_path=state_path, attempts_path=attempts_path, now_ms=1_000_000)
    remaining = json.loads(attempts_path.read_text(encoding="utf-8"))["entries"]

    assert result["pruned_attempt_entries"] == 5
    assert "om_active" in remaining
    assert {key for key in remaining if key.startswith("om_old_")} == {
        "om_old_5",
        "om_old_6",
        "om_old_7",
    }
