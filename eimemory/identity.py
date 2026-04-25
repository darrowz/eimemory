from __future__ import annotations

from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


HONGTU_AGENT_ID = "hongtu"
HONGTU_WORKSPACE_ID = "embodied"
DEFAULT_OPERATOR_USER_ID = "darrow"
OFFICIAL_COMMUNICATION_CHANNEL = "feishu"
EIMEMORY_COMMUNICATION_CHANNEL = "eimemory"
LEGACY_HONGTU_SCOPE_ALIASES: tuple[tuple[str, str], ...] = (
    ("main", ""),
    ("main", "repo-x"),
    ("honxin", "honjia"),
    ("eibrain", "robot"),
)


def hongtu_scope(scope: dict[str, Any] | None) -> dict[str, str]:
    payload = dict(scope or {})
    user_id = _scope_user_id(payload, preserve_blank_user=False)
    return {
        "tenant_id": str(payload.get("tenant_id") or payload.get("tenantId") or "default"),
        "agent_id": HONGTU_AGENT_ID,
        "workspace_id": HONGTU_WORKSPACE_ID,
        "user_id": str(user_id or DEFAULT_OPERATOR_USER_ID),
    }


def hongtu_scope_preserving_user(scope: ScopeRef | dict[str, Any] | None) -> dict[str, str]:
    payload = _scope_payload(scope)
    user_id = _scope_user_id(payload, preserve_blank_user=True)
    return {
        "tenant_id": str(payload.get("tenant_id") or payload.get("tenantId") or "default"),
        "agent_id": HONGTU_AGENT_ID,
        "workspace_id": HONGTU_WORKSPACE_ID,
        "user_id": str(user_id or ""),
    }


def is_hongtu_scope(scope: ScopeRef | dict[str, Any] | None) -> bool:
    scope_ref = _scope_ref(scope)
    return scope_ref.agent_id == HONGTU_AGENT_ID and scope_ref.workspace_id == HONGTU_WORKSPACE_ID


def is_legacy_hongtu_scope(scope: ScopeRef | dict[str, Any] | None) -> bool:
    scope_ref = _scope_ref(scope)
    return (scope_ref.agent_id, scope_ref.workspace_id) in LEGACY_HONGTU_SCOPE_ALIASES


def hongtu_query_scopes(scope: ScopeRef | dict[str, Any] | None) -> list[ScopeRef]:
    scope_ref = _scope_ref(scope)
    if not is_hongtu_scope(scope_ref):
        return [scope_ref]
    candidates = [
        scope_ref,
        *[
            ScopeRef(
                tenant_id=scope_ref.tenant_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                user_id=scope_ref.user_id,
            )
            for agent_id, workspace_id in LEGACY_HONGTU_SCOPE_ALIASES
        ],
    ]
    deduped: list[ScopeRef] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in candidates:
        key = (item.tenant_id, item.agent_id, item.workspace_id, item.user_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


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
    if "eimemory" in lowered:
        return EIMEMORY_COMMUNICATION_CHANNEL
    return "unknown"


def needs_hongtu_identity_repair(record: RecordEnvelope) -> bool:
    if is_legacy_hongtu_scope(record.scope):
        return True
    return is_hongtu_scope(record.scope) and str(record.meta.get("identity") or "") != "hongtu"


def normalize_hongtu_record(record: RecordEnvelope) -> RecordEnvelope:
    normalized = RecordEnvelope.from_dict(record.to_dict())
    previous_scope = normalized.scope
    normalized.scope = ScopeRef.from_dict(hongtu_scope_preserving_user(previous_scope))
    normalized.meta = _normalized_meta(normalized, previous_scope=previous_scope)
    normalized.touch()
    return normalized


def build_identity_report(records: list[RecordEnvelope]) -> dict[str, Any]:
    report = {
        "total_records": len(records),
        "hongtu_scope_records": 0,
        "legacy_scope_records": 0,
        "hongtu_identity_records": 0,
        "repair_candidate_count": 0,
        "channel_roles": {},
        "modalities": {},
        "sample_candidates": [],
    }
    for record in records:
        if is_hongtu_scope(record.scope):
            report["hongtu_scope_records"] += 1
        if is_legacy_hongtu_scope(record.scope):
            report["legacy_scope_records"] += 1
        if str(record.meta.get("identity") or "") == "hongtu":
            report["hongtu_identity_records"] += 1
        role = str(record.meta.get("communication_channel_role") or "")
        if role:
            report["channel_roles"][role] = int(report["channel_roles"].get(role, 0)) + 1
        modality = str(record.meta.get("modality") or record.content.get("modality") or "")
        if modality:
            report["modalities"][modality] = int(report["modalities"].get(modality, 0)) + 1
        if needs_hongtu_identity_repair(record):
            report["repair_candidate_count"] += 1
            if len(report["sample_candidates"]) < 20:
                report["sample_candidates"].append(
                    {
                        "record_id": record.record_id,
                        "kind": record.kind,
                        "title": record.title,
                        "source": record.source,
                        "scope": {
                            "tenant_id": record.scope.tenant_id,
                            "agent_id": record.scope.agent_id,
                            "workspace_id": record.scope.workspace_id,
                            "user_id": record.scope.user_id,
                        },
                    }
                )
    return report


def _scope_payload(scope: ScopeRef | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(scope, ScopeRef):
        return {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        }
    return dict(scope or {})


def _scope_ref(scope: ScopeRef | dict[str, Any] | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _scope_user_id(payload: dict[str, Any], *, preserve_blank_user: bool) -> str:
    user_id = (
        payload.get("user_id")
        or payload.get("userId")
        or payload.get("actor_id")
        or payload.get("actorId")
        or payload.get("operator_id")
        or payload.get("operatorId")
    )
    if preserve_blank_user:
        return str(user_id or "")
    return str(user_id or DEFAULT_OPERATOR_USER_ID)


def _normalized_meta(record: RecordEnvelope, *, previous_scope: ScopeRef) -> dict[str, Any]:
    meta = dict(record.meta or {})
    source = str(record.source or "")
    channel = str(meta.get("communication_channel") or _channel_from_source(source))
    organ = str(meta.get("organ") or _organ_from_record(record))
    modality = str(meta.get("modality") or _modality_from_record(record))
    hardware_node = str(meta.get("hardware_node") or _hardware_node_from_record(record, previous_scope=previous_scope))
    identity_meta = hongtu_identity_meta(
        source=source,
        channel=channel,
        hardware_node=hardware_node,
        organ=organ,
        modality=modality,
        extra={
            "runtime_node": str(meta.get("runtime_node") or previous_scope.agent_id or ""),
            **({"official_channel": True} if channel == OFFICIAL_COMMUNICATION_CHANNEL else {}),
        },
    )
    meta.update(identity_meta)
    return meta


def _hardware_node_from_record(record: RecordEnvelope, *, previous_scope: ScopeRef) -> str:
    if previous_scope.agent_id in {"honjia", "honxin"}:
        return previous_scope.agent_id
    lowered_source = str(record.source or "").lower()
    if lowered_source.startswith("openclaw.") or lowered_source.startswith("eimemory."):
        return "honxin"
    if lowered_source.startswith("eibrain."):
        return "honxin"
    return "honxin"


def _organ_from_record(record: RecordEnvelope) -> str:
    lowered_source = str(record.source or "").lower()
    if lowered_source.startswith("openclaw.") or lowered_source.startswith("eibrain."):
        return "cognition"
    if lowered_source.startswith("eimemory."):
        return "memory"
    if record.kind == "recall_view":
        return "cognition"
    return "memory"


def _modality_from_record(record: RecordEnvelope) -> str:
    if str(record.meta.get("modality") or "").strip():
        return str(record.meta.get("modality") or "")
    if str(record.content.get("modality") or "").strip():
        return str(record.content.get("modality") or "")
    text_like_kinds = {
        "memory",
        "recall_view",
        "knowledge_page",
        "knowledge_candidate",
        "claim_card",
        "paper_source",
        "paper_extract",
        "entity_record",
        "relation_record",
        "source_candidate",
    }
    if record.kind in text_like_kinds:
        return "text"
    return ""
