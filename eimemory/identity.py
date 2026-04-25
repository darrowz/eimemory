from __future__ import annotations

from typing import Any


HONGTU_AGENT_ID = "hongtu"
HONGTU_WORKSPACE_ID = "embodied"
DEFAULT_OPERATOR_USER_ID = "darrow"
OFFICIAL_COMMUNICATION_CHANNEL = "feishu"


def hongtu_scope(scope: dict[str, Any] | None) -> dict[str, str]:
    payload = dict(scope or {})
    user_id = (
        payload.get("user_id")
        or payload.get("userId")
        or payload.get("actor_id")
        or payload.get("actorId")
        or payload.get("operator_id")
        or payload.get("operatorId")
        or DEFAULT_OPERATOR_USER_ID
    )
    return {
        "tenant_id": str(payload.get("tenant_id") or payload.get("tenantId") or "default"),
        "agent_id": HONGTU_AGENT_ID,
        "workspace_id": HONGTU_WORKSPACE_ID,
        "user_id": str(user_id or DEFAULT_OPERATOR_USER_ID),
    }


def hongtu_identity_meta(
    *,
    source: str,
    channel: str = "",
    hardware_role: str = "head",
    hardware_id: str = "head-v0",
    hardware_node: str = "",
    organ: str = "",
    modality: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    channel_name = channel or _channel_from_source(source)
    meta = {
        "identity": "hongtu",
        "memory_subject": "hongtu",
        "embodiment": hardware_id,
        "hardware_role": hardware_role,
        "hardware_id": hardware_id,
        "hardware_node": hardware_node,
        "organ": organ,
        "modality": modality,
        "source_channel": channel_name,
        "communication_channel": channel_name,
        "communication_channel_role": "official" if channel_name == OFFICIAL_COMMUNICATION_CHANNEL else "auxiliary",
    }
    if extra:
        meta.update({str(key): value for key, value in extra.items()})
    return meta


def _channel_from_source(source: str) -> str:
    lowered = str(source or "").lower()
    if "feishu" in lowered or "openclaw" in lowered:
        return OFFICIAL_COMMUNICATION_CHANNEL
    if "eibrain" in lowered:
        return "eibrain"
    return "unknown"
