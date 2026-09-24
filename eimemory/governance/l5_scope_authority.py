"""Which L5 facts may be read from the product deployment scope.

Provider, catalog, advertisement, and release-identity evidence belong to the
deployed product. A Feishu or Hermes channel of that same operator may read
them. Recall samples, business receipts, and quality-repair observations stay
on the scope that produced them.
"""

from __future__ import annotations

from collections.abc import Mapping

from eimemory.adapters.runtime.channel import (
    SUPPORTED_RUNTIME_CHANNELS,
    base_scope_from_channel,
    runtime_channel_from_scope,
)
from eimemory.identity import (
    _CANONICAL_HONGTU_USER_ALIASES,
    canonical_hongtu_user_id,
    default_operator_user_id,
    refresh_identity_from_env,
)
from eimemory.models.records import ScopeRef


_CHANNEL_WORKSPACES = tuple(
    sorted(channel for channel in SUPPORTED_RUNTIME_CHANNELS if channel != "openclaw")
)
SHARED_CAPABILITY_EVIDENCE = (
    "provider",
    "catalog",
    "advertisement",
    "release_identity",
)
REPORT_SCOPE_EVIDENCE = (
    "recall_quality",
    "business_receipt",
    "quality_repair",
)


def evidence_partition() -> dict[str, list[str]]:
    return {
        "shared_capability": list(SHARED_CAPABILITY_EVIDENCE),
        "report_scope_required": list(REPORT_SCOPE_EVIDENCE),
    }


def _scope_ref(scope: ScopeRef | Mapping[str, object] | None) -> ScopeRef:
    if isinstance(scope, ScopeRef):
        return scope
    return ScopeRef.from_dict(dict(scope or {}))


def _operator_user_ids(user_id: str) -> tuple[str, ...]:
    """Alias ids of this operator only. An unrelated user stays alone."""

    text = str(user_id or "").strip()
    if not text:
        return ()
    refresh_identity_from_env()
    canonical = canonical_hongtu_user_id(text)
    known = text.casefold() in {
        alias.casefold()
        for aliases in _CANONICAL_HONGTU_USER_ALIASES.values()
        for alias in aliases
    }
    if canonical == text and not known and text != default_operator_user_id():
        return (text,)
    aliases = _CANONICAL_HONGTU_USER_ALIASES.get(canonical) or (canonical,)
    ordered: list[str] = []
    for item in (canonical, *aliases, text):
        if item and item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def authorized_capability_scopes(scope: ScopeRef | Mapping[str, object] | None) -> list[ScopeRef]:
    """Product scopes that may share capability and release evidence.

    The requested scope is always first. Channel workspaces and the same
    operator's configured aliases follow. Other tenants, agents, and users
    are not searched.
    """

    origin = _scope_ref(scope)
    channel = runtime_channel_from_scope(origin)
    workspaces = [origin.workspace_id]
    if channel in _CHANNEL_WORKSPACES:
        base = base_scope_from_channel(channel, origin)
        if base["workspace_id"] not in workspaces:
            workspaces.append(base["workspace_id"])
    elif origin.workspace_id and "::channel::" not in origin.workspace_id:
        for name in _CHANNEL_WORKSPACES:
            suffixed = f"{origin.workspace_id}::channel::{name}"
            if suffixed not in workspaces:
                workspaces.append(suffixed)
    users = _operator_user_ids(origin.user_id) or (origin.user_id,)
    scopes: list[ScopeRef] = []
    seen: set[tuple[str, str, str, str]] = set()
    for workspace_id in workspaces:
        for user_id in users:
            candidate = ScopeRef(
                tenant_id=origin.tenant_id,
                agent_id=origin.agent_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            key = (candidate.tenant_id, candidate.agent_id, candidate.workspace_id, candidate.user_id)
            if key in seen:
                continue
            seen.add(key)
            scopes.append(candidate)
    return scopes


__all__ = [
    "REPORT_SCOPE_EVIDENCE",
    "SHARED_CAPABILITY_EVIDENCE",
    "authorized_capability_scopes",
    "evidence_partition",
]
