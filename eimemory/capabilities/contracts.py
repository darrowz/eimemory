from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any

CAPABILITY_SCHEMA_VERSION = "capability.v3"
MAX_IDENTIFIER_CHARS = 256
MAX_TEXT_CHARS = 8_192
MAX_COLLECTION_ITEMS = 256
MAX_PAYLOAD_BYTES = 262_144
MAX_JSON_DEPTH = 32

_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)
_EXECUTABLE_KEYS = frozenset(
    {
        "argv",
        "bash",
        "cmd",
        "command",
        "command_line",
        "commandline",
        "exec",
        "executable_path",
        "executable",
        "powershell",
        "process",
        "script_path",
        "shell_command",
        "script",
        "shell",
        "subprocess",
    }
)


class CapabilityContractError(ValueError):
    """Raised when an untrusted capability contract is invalid or unsafe."""


def contract_digest(payload: Mapping[str, Any] | dict[str, Any]) -> str:
    """Return the stable SHA-256 digest of a validated JSON payload."""

    normalized = normalize_json_payload(payload, field="payload")
    return sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def canonical_json(payload: Mapping[str, Any] | dict[str, Any]) -> str:
    """Serialize a capability payload without importing a storage owner."""

    normalized = normalize_json_payload(payload, field="payload")
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_capability_id(value: object, *, field: str = "capability_id") -> str:
    text = _required_text(value, field=field, max_chars=MAX_IDENTIFIER_CHARS)
    if not _CAPABILITY_ID_RE.fullmatch(text):
        raise CapabilityContractError(
            f"{field} must be lowercase dot-separated capability identity"
        )
    return text


def normalize_opaque_id(value: object, *, field: str) -> str:
    text = _required_text(value, field=field, max_chars=MAX_IDENTIFIER_CHARS)
    if not _OPAQUE_ID_RE.fullmatch(text):
        raise CapabilityContractError(f"{field} contains unsupported characters")
    return text


def normalize_text(value: object, *, field: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    return _required_text(value, field=field, max_chars=max_chars)


def normalize_optional_text(
    value: object,
    *,
    field: str,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CapabilityContractError(f"{field} must be text")
    text = value.strip()
    if len(text) > max_chars:
        raise CapabilityContractError(f"{field} exceeds {max_chars} characters")
    return text


def normalize_sha256(value: object, *, field: str) -> str:
    text = _required_text(value, field=field, max_chars=64).lower()
    if not _SHA256_RE.fullmatch(text):
        raise CapabilityContractError(f"{field} must be a SHA-256 digest")
    return text


def normalize_string_sequence(
    values: Sequence[object] | object,
    *,
    field: str,
    item_field: str | None = None,
    max_items: int = MAX_COLLECTION_ITEMS,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise CapabilityContractError(f"{field} must be a sequence of text values")
    if len(values) > max_items:
        raise CapabilityContractError(f"{field} exceeds {max_items} items")
    normalized: list[str] = []
    for item in values:
        text = _required_text(item, field=item_field or field, max_chars=MAX_IDENTIFIER_CHARS)
        if text not in normalized:
            normalized.append(text)
    return tuple(sorted(normalized) if sort else normalized)


def normalize_json_payload(
    value: Mapping[str, Any] | dict[str, Any],
    *,
    field: str,
    reject_executable: bool = False,
    max_bytes: int = MAX_PAYLOAD_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityContractError(f"{field} must be an object")
    normalized = _normalize_json_value(
        value,
        field=field,
        reject_executable=reject_executable,
        depth=0,
    )
    if not isinstance(normalized, dict):  # defensive: mappings normalize to dict
        raise CapabilityContractError(f"{field} must be an object")
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise CapabilityContractError(f"{field} exceeds {max_bytes} bytes")
    return normalized


def require_timestamp(value: object, *, field: str, required: bool = True) -> str:
    text = normalize_optional_text(value, field=field, max_chars=128)
    if required and not text:
        raise CapabilityContractError(f"{field} is required")
    if not text:
        return ""
    if not _RFC3339_UTC_RE.fullmatch(text):
        raise CapabilityContractError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityContractError(f"{field} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CapabilityContractError(f"{field} must be an RFC3339 UTC timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def ensure_timestamp_order(
    earlier: str,
    later: str,
    *,
    earlier_field: str,
    later_field: str,
) -> None:
    """Reject an event interval whose endpoint precedes its start."""

    if earlier and later and later < earlier:
        raise CapabilityContractError(f"{later_field} must not precede {earlier_field}")


def ensure_allowed(value: object, *, field: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field=field, max_chars=MAX_IDENTIFIER_CHARS)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise CapabilityContractError(f"{field} must be one of: {choices}")
    return text


def ensure_probability(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityContractError(f"{field} must be a number from 0 to 1")
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise CapabilityContractError(f"{field} must be a number from 0 to 1")
    return result


def _required_text(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise CapabilityContractError(f"{field} must be text")
    text = value.strip()
    if not text:
        raise CapabilityContractError(f"{field} is required")
    if len(text) > max_chars:
        raise CapabilityContractError(f"{field} exceeds {max_chars} characters")
    return text


def _normalize_json_value(
    value: Any,
    *,
    field: str,
    reject_executable: bool,
    depth: int,
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise CapabilityContractError(f"{field} exceeds maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapabilityContractError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapabilityContractError(f"{field} exceeds {MAX_COLLECTION_ITEMS} items")
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CapabilityContractError(f"{field} contains a non-text key")
            key = raw_key.strip()
            if not key:
                raise CapabilityContractError(f"{field} contains an empty key")
            if len(key) > MAX_IDENTIFIER_CHARS:
                raise CapabilityContractError(f"{field} contains an oversized key")
            if reject_executable and key.lower() in _EXECUTABLE_KEYS:
                raise CapabilityContractError(f"{field} contains executable key: {key}")
            if key in result:
                raise CapabilityContractError(f"{field} contains duplicate normalized key: {key}")
            result[key] = _normalize_json_value(
                item,
                field=f"{field}.{key}",
                reject_executable=reject_executable,
                depth=depth + 1,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapabilityContractError(f"{field} exceeds {MAX_COLLECTION_ITEMS} items")
        return [
            _normalize_json_value(
                item,
                field=f"{field}[]",
                reject_executable=reject_executable,
                depth=depth + 1,
            )
            for item in value
        ]
    raise CapabilityContractError(f"{field} contains unsupported JSON value")
