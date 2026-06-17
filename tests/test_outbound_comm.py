"""Tests for outbound comm audit logging (Task 4.1).

The outbound comm module is the gate that lets the autonomous loop send
messages to the outside world. Two safety properties must hold:

1. Allowed (free) channels log a row to the audit JSONL.
2. Paid channels (twilio, sendgrid, mailgun, postmark) are blocked at the
   call site and raise ``ChannelBlocked`` — this is the no-spend rule
   carried over from the 2026-06-17 spec.
"""
from __future__ import annotations

import json
from pathlib import Path

from eimemory.governance.safety.outbound_comm import ChannelBlocked, OutboundComm


def test_free_channel_allowed_and_logged(tmp_path: Path) -> None:
    oc = OutboundComm(root=tmp_path, allowed_channels=["telegram", "openclaw_bus"])
    oc.send("telegram", "hongtu", "hi")
    history = oc.history()
    assert len(history) == 1
    row = history[0]
    assert row["channel"] == "telegram"
    assert row["recipient"] == "hongtu"
    assert "ts" in row
    assert "payload_hash" in row


def test_paid_channel_blocked_with_reason(tmp_path: Path) -> None:
    oc = OutboundComm(root=tmp_path, allowed_channels=["telegram"])
    try:
        oc.send("twilio", "+8612345", "test")
    except ChannelBlocked as e:
        assert e.channel == "twilio"
        assert "paid" in e.reason.lower()
    else:
        raise AssertionError("twilio should have raised ChannelBlocked")
    # Nothing should have been written to the audit log.
    log_path = tmp_path / "outbound_comm.jsonl"
    if log_path.exists():
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert rows == []
