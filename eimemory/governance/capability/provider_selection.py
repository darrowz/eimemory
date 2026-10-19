"""Explicit, code-only Profile read delegation; evidence ownership never moves."""
from __future__ import annotations
from dataclasses import asdict
from collections.abc import Mapping
from eimemory.models.records import ScopeRef

def provider_selection(runtime, *, owner_scope, profile_key: str, snapshot=None) -> tuple[ScopeRef, dict]:
    owner = owner_scope if isinstance(owner_scope, ScopeRef) else ScopeRef.from_dict(owner_scope)
    if snapshot is None:
        return owner, {}
    from eimemory.governance.evolution.code_evolution_provider_authority import verify_provider_authority
    if not profile_key:
        raise ValueError("provider_selection_profile_required")
    try:
        authority = verify_provider_authority(runtime, snapshot, owner_scope=owner, profile_key=profile_key)
    except Exception as exc:
        raise ValueError("provider_selection_authority_unavailable:" + type(exc).__name__) from exc
    provider = ScopeRef.from_dict(authority["provider_scope"])
    return provider, {"provider_authority_snapshot": authority,
                      "provider_authority_digest": authority["digest"], "provider_scope": asdict(provider)}

def verify_selection_authority(runtime, contract: Mapping, *, owner_scope) -> dict:
    keys = {"provider_authority_snapshot", "provider_authority_digest", "provider_scope"}
    present = keys.intersection(contract)
    delegated = contract.get("schema") == "capability_replay_selection.v2"
    if not present:
        if delegated:
            raise ValueError("provider_selection_authority_required")
        return {}
    if not delegated:
        raise ValueError("provider_selection_authority_schema_invalid")
    if present != keys or contract.get("provider_authority_snapshot") is None:
        raise ValueError("provider_selection_authority_incomplete")
    _, actual = provider_selection(runtime, owner_scope=owner_scope,
        profile_key=str(contract.get("profile_key") or ""), snapshot=contract["provider_authority_snapshot"])
    if any(contract.get(key) != actual[key] for key in keys):
        raise ValueError("provider_selection_authority_mismatch")
    return actual
