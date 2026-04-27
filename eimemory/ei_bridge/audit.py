from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from time import time
from typing import Any

from .protocol import BridgeCommand, BridgeResult


MAX_INLINE_PAYLOAD_BYTES = 1024
MAX_INLINE_FIELD_BYTES = 256
MAX_INLINE_SEQUENCE_ITEMS = 8


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _stable_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _is_small_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return len(value.encode("utf-8")) <= MAX_INLINE_FIELD_BYTES
    return isinstance(value, (int, float, bool)) or value is None


def _is_small_sequence(value: Any) -> bool:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return False
    if len(value) > MAX_INLINE_SEQUENCE_ITEMS:
        return False
    return all(_is_small_scalar(item) for item in value) and len(_stable_json(value).encode("utf-8")) <= MAX_INLINE_FIELD_BYTES


def _is_small_field(value: Any) -> bool:
    return _is_small_scalar(value) or _is_small_sequence(value)


def _summarize_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    if not payload:
        return {}, None

    json_size = len(_stable_json(payload).encode("utf-8"))
    if json_size <= MAX_INLINE_PAYLOAD_BYTES and all(_is_small_field(value) for value in payload.values()):
        return dict(payload), _digest_payload(payload)

    keys = sorted(str(key) for key in payload.keys())
    sample = {
        str(key): payload[key]
        for key in sorted(payload, key=str)
        if _is_small_field(payload[key])
    }
    return (
        {
            "field_count": len(payload),
            "keys": keys,
            "sample": sample,
            "types": {str(key): type(value).__name__ for key, value in payload.items()},
        },
        _digest_payload(payload),
    )


def _source_with_channel(command: BridgeCommand, channel: str | None) -> dict[str, Any]:
    source = command.source.to_dict()
    if channel is not None:
        source["channel"] = channel
    return source


def should_persist(command: BridgeCommand, result: BridgeResult) -> bool:
    if command.policy.get("audit") is False:
        return False
    if not result.ok:
        return True
    return bool(result.summary or result.error or result.payload or result.audit)


def build_audit_record(command: BridgeCommand, result: BridgeResult, *, channel: str | None = None) -> dict[str, Any]:
    payload, payload_digest = _summarize_payload(result.payload)
    completed_at_ts = result.audit.get("completed_at_ts", time())
    record = {
        "type": "ei_bridge.audit",
        "command_id": command.command_id,
        "source": _source_with_channel(command, channel),
        "target": command.target.to_dict(),
        "intent": command.intent,
        "ok": result.ok,
        "summary": result.summary,
        "error": result.error,
        "created_at_ts": command.created_at_ts,
        "completed_at_ts": completed_at_ts,
        "payload": payload,
        "policy": dict(command.policy),
    }
    if payload_digest is not None:
        record["payload_digest"] = payload_digest
    return record


class EIMemoryAuditSink:
    def __init__(self, writer: Callable[[dict[str, Any]], Any]) -> None:
        self.writer = writer

    def record(self, command: BridgeCommand, result: BridgeResult) -> BridgeResult:
        if not should_persist(command, result):
            return BridgeResult(
                ok=True,
                command_id=command.command_id,
                summary="audit skipped",
                payload={"persisted": False},
            )

        record = build_audit_record(command, result)
        try:
            writer_result = self.writer(record)
        except Exception as exc:  # pragma: no cover - exact exception type belongs to the injected writer.
            return BridgeResult(
                ok=False,
                command_id=command.command_id,
                summary=f"audit writer failed: {exc}",
                error="audit_writer_error",
            )

        if isinstance(writer_result, BridgeResult):
            return writer_result
        payload = writer_result if isinstance(writer_result, dict) else {"writer_result": writer_result}
        return BridgeResult(
            ok=True,
            command_id=command.command_id,
            summary="audit recorded",
            payload=payload,
        )


__all__ = ["EIMemoryAuditSink", "build_audit_record", "should_persist"]
