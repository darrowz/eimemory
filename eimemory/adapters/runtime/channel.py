from __future__ import annotations

from dataclasses import asdict

from eimemory.models.records import ScopeRef


RUNTIME_ADAPTER_CONTRACT_VERSION = "agent.runtime.v1"
AUTHORITY_MODE = "per_channel"
SUPPORTED_RUNTIME_CHANNELS = frozenset({"openclaw", "codex", "hermes"})
_CHANNEL_ALIASES = {
    "open-claw": "openclaw",
    "open_claw": "openclaw",
    "codex-cli": "codex",
    "codex_cli": "codex",
    "hermes-agent": "hermes",
    "hermes_agent": "hermes",
}
_CHANNEL_SCOPE_SEPARATOR = "::channel::"


def normalize_runtime_channel(channel: str) -> str:
    value = str(channel or "").strip().lower()
    value = _CHANNEL_ALIASES.get(value, value)
    if value not in SUPPORTED_RUNTIME_CHANNELS:
        raise ValueError(f"unsupported runtime channel: {channel}")
    return value


def resolve_channel_scope(channel: str, scope: dict | ScopeRef | None) -> dict[str, str]:
    channel_id = normalize_runtime_channel(channel)
    resolved = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    payload = asdict(resolved)
    if channel_id == "openclaw":
        return payload
    suffix = f"{_CHANNEL_SCOPE_SEPARATOR}{channel_id}"
    workspace_id = resolved.workspace_id or "default"
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
