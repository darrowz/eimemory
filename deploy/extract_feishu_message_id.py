#!/usr/bin/env python3
"""Extract a platform-accepted Feishu message ID from bounded JSON."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


MAX_RECEIPT_BYTES = 1024 * 1024
_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]{6,128}\Z")


def _path(payload: object, *keys: str) -> object:
    value = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def extract_feishu_message_id(payload: object) -> str:
    """Return the first supported, syntactically valid message ID."""

    if isinstance(payload, dict) and payload.get("ok") is False:
        return ""
    candidates = (
        _path(payload, "messageId"),
        _path(payload, "message_id"),
        _path(payload, "primaryPlatformMessageId"),
        _path(payload, "payload", "messageId"),
        _path(payload, "payload", "message_id"),
        _path(payload, "payload", "receipt", "primaryPlatformMessageId"),
        _path(payload, "data", "messageId"),
        _path(payload, "data", "message_id"),
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            message_id = candidate.strip()
            if _MESSAGE_ID.fullmatch(message_id):
                return message_id
    return ""


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        print("Feishu receipt exceeds size limit", file=sys.stderr)
        return 2
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        print("Feishu receipt is not valid JSON", file=sys.stderr)
        return 2
    message_id = extract_feishu_message_id(payload)
    if not message_id:
        print("Feishu receipt has no supported message ID", file=sys.stderr)
        return 1
    print(message_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
