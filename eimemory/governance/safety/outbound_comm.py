"""Audit-logged outbound comm. Free channels only.

Per the 2026-06-17 spec, the autonomous loop may only call free
communication channels (telegram, openclaw internal bus, local SMTP,
self-hosted webhook). Paid channels (twilio, sendgrid, mailgun,
postmark) are blocked at this layer so a misconfigured caller cannot
incidentally spend money.

Every allowed send is appended to ``outbound_comm.jsonl`` under
``root/`` as a single JSON row with ``ts`` (UTC ISO 8601), ``channel``,
``recipient`` and ``payload_hash``. The hash is a stable Python
``hash()`` of the payload string, so the log never contains the raw
message body — auditors can verify delivery without leaking content.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


PAID_CHANNELS = {"twilio", "sendgrid", "mailgun", "postmark"}


class ChannelBlocked(Exception):
    """Raised when a caller tries to use a paid or non-allowlisted channel."""

    def __init__(self, channel: str, reason: str):
        self.channel = channel
        self.reason = reason
        super().__init__(f"channel_blocked: {channel} ({reason})")


class OutboundComm:
    """Audit-logged outbound comm dispatcher.

    Args:
        root: Directory where ``outbound_comm.jsonl`` will be written.
            Created on first use.
        allowed_channels: Set of free channels the caller is allowed to
            use. Anything not in this set AND not in ``PAID_CHANNELS``
            is blocked with reason ``channel X not in allow-list``.
    """

    def __init__(self, root: Path, allowed_channels: list[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "outbound_comm.jsonl"
        self.allowed = set(allowed_channels)

    def send(self, channel: str, recipient: str, payload: str) -> None:
        """Send ``payload`` to ``recipient`` over ``channel`` and audit-log it.

        Raises:
            ChannelBlocked: if ``channel`` is in ``PAID_CHANNELS`` or
                is not in the allow-list. No row is written in that case.
        """
        if channel in PAID_CHANNELS:
            raise ChannelBlocked(channel, "paid channels blocked per no-spend rule")
        if channel not in self.allowed:
            raise ChannelBlocked(channel, f"channel {channel} not in allow-list")
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "recipient": recipient,
            "payload_hash": hash(payload),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def history(self, n: int = 100) -> list[dict]:
        """Return the last ``n`` audit rows, in append order (oldest of the slice first)."""
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # Skip corrupt lines rather than fail the audit read.
                    continue
        return rows[-n:]
