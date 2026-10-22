"""Pure immutable code-provider authority encoding for the data plane.

Selection and current root/config/Profile authorization remain in governance.
This module only checks canonical snapshot/transaction projections; it opens
no configuration, capability registry, database, provider or runtime.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.models.records import ScopeRef

SCHEMA = 'code_provider_authority.v1'
SOURCE_KEY = 'capability_authority'
CAPABILITY_ID = 'code.implementation'
OPERATION = 'propose_patch_v2'
_SCOPE_KEYS = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
_SNAPSHOT_KEYS = {'schema', 'owner_scope', 'provider_scope', 'capability_id', 'capability_scope',
                  'operation', 'profile_key', 'profile_digest', 'authorization_basis', 'delegation_digest', 'digest'}


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                            allow_nan=False).encode()).hexdigest()


def _scope(value: ScopeRef | Mapping[str, Any]) -> dict[str, str]:
    result = asdict(value) if isinstance(value, ScopeRef) else dict(value)
    if (set(result) != _SCOPE_KEYS or any(not isinstance(item, str) or not item or item != item.strip() or '*' in item
            or any(ord(char) < 32 for char in item) for item in result.values())):
        raise ValueError('code_provider_exact_scope_required')
    return result


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def validate_provider_authority(snapshot: Any, *, owner_scope: ScopeRef | Mapping[str, Any] | None = None) -> dict:
    """Validate immutable encoding only; current authorization is separate."""
    if (not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_KEYS or snapshot.get('schema') != SCHEMA
            or snapshot.get('capability_id') != CAPABILITY_ID or snapshot.get('capability_scope') != 'global'
            or snapshot.get('operation') != OPERATION or not isinstance(snapshot.get('profile_key'), str)
            or not snapshot['profile_key'] or snapshot['profile_key'] != snapshot['profile_key'].strip()
            or not _hex64(snapshot.get('profile_digest'))):
        raise ValueError('code_provider_authority_snapshot_invalid')
    owner = _scope(snapshot['owner_scope']);provider = _scope(snapshot['provider_scope'])
    if (owner_scope is not None and owner != _scope(owner_scope)
            or owner['tenant_id'] != provider['tenant_id'] or owner['agent_id'] != provider['agent_id']):
        raise ValueError('code_provider_authority_owner_mismatch')
    basis = snapshot.get('authorization_basis');delegation = snapshot.get('delegation_digest')
    if (basis not in {'exact_scope', 'existing_product_scope', 'root_code_provider_delegation', 'user_code_provider_delegation'}
            or (basis == 'exact_scope') != (owner == provider)
            or (not _hex64(delegation) if basis in {'root_code_provider_delegation', 'user_code_provider_delegation'} else delegation != '')
            or snapshot.get('digest') != _digest({key: value for key, value in snapshot.items() if key != 'digest'})):
        raise ValueError('code_provider_authority_digest_invalid')
    return dict(snapshot)


def validate_provider_authority_snapshot(snapshot: Any, *, owner_scope: ScopeRef | Mapping[str, Any],
                                         profile_key: str = '') -> dict:
    value = validate_provider_authority(snapshot, owner_scope=owner_scope)
    if profile_key and value['profile_key'] != profile_key:
        raise ValueError('code_provider_authority_profile_key_mismatch')
    return value


def transaction_provider_authority(transaction: Mapping[str, Any]) -> dict | None:
    """Read the same snapshot from source, both ledger mirrors and proposal."""
    source = transaction.get('source_evidence') or {}
    payload = transaction.get('payload') or {}
    proposal = payload.get('payload') if isinstance(payload, Mapping) else None
    mirror = payload.get('source_evidence') if isinstance(payload, Mapping) else None
    values = [source.get(SOURCE_KEY) if isinstance(source, Mapping) else None,
              payload.get(SOURCE_KEY) if isinstance(payload, Mapping) else None,
              proposal.get(SOURCE_KEY) if isinstance(proposal, Mapping) else None,
              mirror.get(SOURCE_KEY) if isinstance(mirror, Mapping) else None]
    if all(value is None for value in values):
        if (transaction.get('schema_version') == 'code_evolution_transaction.v3'
                or any(isinstance(item, Mapping) and SOURCE_KEY in item for item in (source,payload,proposal,mirror))):
            raise ValueError('code_provider_authority_snapshot_missing')
        return None
    if any(value is None or value != values[0] for value in values):
        raise ValueError('code_provider_authority_projection_mismatch')
    owner = {key: transaction[key] for key in _SCOPE_KEYS}
    return validate_provider_authority_snapshot(values[0], owner_scope=owner,
        profile_key=str(transaction.get('profile_key') or ''))
