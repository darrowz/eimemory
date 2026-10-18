from __future__ import annotations

from dataclasses import asdict

from eimemory.contracts.recall_boundary import (
    CHANNEL_SCOPE_SEPARATOR as _CHANNEL_SCOPE_SEPARATOR,
    SUPPORTED_RUNTIME_CHANNELS,
    normalize_runtime_channel,
    runtime_channel_from_workspace,
)
from eimemory.models.records import ScopeRef


RUNTIME_ADAPTER_CONTRACT_VERSION = "agent.runtime.v1"
AUTHORITY_MODE = "per_channel"


def resolve_channel_scope(channel: str, scope: dict | ScopeRef | None) -> dict[str, str]:
    channel_id = normalize_runtime_channel(channel)
    resolved = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    payload = asdict(resolved)
    if channel_id == "openclaw":
        return payload
    suffix = f"{_CHANNEL_SCOPE_SEPARATOR}{channel_id}"
    workspace_id = resolved.workspace_id
    if not workspace_id.endswith(suffix):
        workspace_id += suffix
    payload["workspace_id"] = workspace_id
    return payload


def base_scope_from_channel(channel: str, scope: dict | ScopeRef | None) -> dict[str, str]:
    channel_id = normalize_runtime_channel(channel)
    resolved = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    payload = asdict(resolved)
    if channel_id == "openclaw":
        return payload
    suffix = f"{_CHANNEL_SCOPE_SEPARATOR}{channel_id}"
    workspace_id = resolved.workspace_id
    if workspace_id.endswith(suffix):
        workspace_id = workspace_id[: -len(suffix)]
    payload["workspace_id"] = workspace_id
    return payload


def runtime_channel_from_scope(scope: dict | ScopeRef | None) -> str:
    resolved = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return runtime_channel_from_workspace(resolved.workspace_id)
