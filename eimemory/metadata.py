from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BUSINESS_META_KEY = "business_meta"
RUNTIME_META_KEY = "runtime_meta"

RUNTIME_META_ALIASES: dict[str, str] = {
    "host": "host",
    "host_id": "host_id",
    "hostId": "host_id",
    "hardware_node": "hardware_node",
    "hardwareNode": "hardware_node",
    "hardware_role": "hardware_role",
    "hardwareRole": "hardware_role",
    "hardware_id": "hardware_id",
    "hardwareId": "hardware_id",
    "node_id": "hardware_node",
    "nodeId": "hardware_node",
    "runtime_node": "runtime_node",
    "runtimeNode": "runtime_node",
    "organ": "organ",
    "modality": "modality",
    "service": "service",
    "service_name": "service_name",
    "serviceName": "service_name",
    "trace": "trace",
    "trace_id": "trace_id",
    "traceId": "trace_id",
    "transport": "transport",
    "transport_provider": "transport_provider",
    "transportProvider": "transport_provider",
}
TOP_LEVEL_BUSINESS_META_KEYS: frozenset[str] = frozenset({"quality", "scoring"})


def split_metadata(meta: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = dict(meta or {})
    business_meta = _dict_value(payload.get(BUSINESS_META_KEY))
    for key in TOP_LEVEL_BUSINESS_META_KEYS:
        business_meta.pop(key, None)
    runtime_meta = _dict_value(payload.get(RUNTIME_META_KEY))
    for key, value in payload.items():
        if key in {BUSINESS_META_KEY, RUNTIME_META_KEY}:
            continue
        runtime_key = RUNTIME_META_ALIASES.get(str(key))
        if runtime_key:
            if _has_runtime_value(value):
                runtime_meta[runtime_key] = value
            continue
        business_meta[str(key)] = value
    return business_meta, runtime_meta


def normalize_metadata(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    business_meta, runtime_meta = split_metadata(meta)
    payload = dict(business_meta)
    payload[BUSINESS_META_KEY] = {
        key: value
        for key, value in business_meta.items()
        if key not in TOP_LEVEL_BUSINESS_META_KEYS
    }
    payload[RUNTIME_META_KEY] = dict(runtime_meta)
    return payload


def business_metadata(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    return split_metadata(meta)[0]


def runtime_metadata(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    return split_metadata(meta)[1]


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _has_runtime_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
