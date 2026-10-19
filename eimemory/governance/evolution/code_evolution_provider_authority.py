"""Exact code-provider delegation, separate from memory and incident ownership.

A root-controlled mapping may delegate code.implementation and its necessary
Profile/catalog/advertisement reads. It never changes global identity aliases,
generic capability sharing, receipt authority, repair effects or provenance.
Legacy proposals without a snapshot continue to resolve their own exact scope.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import os
import stat

from eimemory.core.strict_json import loads
from eimemory.models.records import ScopeRef

SCHEMA = 'code_provider_authority.v1'
SOURCE_KEY = 'capability_authority'
DELEGATION_SCHEMA = 'code_provider_scope_delegations.v1'
DELEGATION_PATH = Path('/etc/eimemory-code-automation-authority/code-provider-delegations.v1.json')
CAPABILITY_ID = 'code.implementation'
OPERATION = 'propose_patch_v2'
_SCOPE_KEYS = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
_SNAPSHOT_KEYS = {'schema', 'owner_scope', 'provider_scope', 'capability_id', 'capability_scope',
                  'operation', 'profile_key', 'profile_digest', 'authorization_basis', 'delegation_digest', 'digest'}
_ENTRY_KEYS = {'owner_scope', 'provider_scope', 'capability_id', 'capability_scope', 'operation',
               'profile_key', 'profile_digest'}


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


def configured_provider_scope(owner_scope: ScopeRef | Mapping[str, Any]) -> dict[str, str]:
    """The existing deployment selector coordinates, without granting access."""
    from eimemory.config.loader import load_settings
    from eimemory.identity import default_operator_user_id
    owner = _scope(owner_scope)
    settings = load_settings()
    return _scope({'tenant_id': owner['tenant_id'],
        'agent_id': str(os.environ.get('EIMEMORY_AGENT_ID') or os.environ.get('EIMEMORY_DEPLOY_SCOPE_AGENT')
                        or settings.default_agent_id),
        'workspace_id': str(os.environ.get('EIMEMORY_WORKSPACE_ID') or os.environ.get('EIMEMORY_DEPLOY_SCOPE_WORKSPACE')
                            or settings.default_workspace_id),
        'user_id': default_operator_user_id()})


def _secure_delegations() -> dict | None:
    from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
    path = DELEGATION_PATH
    if not os.path.lexists(path):
        return None
    if os.name != 'posix' or _v2_no_symlink(path, missing_ok=False):
        raise ValueError('code_provider_delegation_path_invalid')
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (info.st_mode & 0o022 and not (parent != path.parent and info.st_mode & stat.S_ISVTX))):
            raise ValueError('code_provider_delegation_parent_not_root_controlled')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(descriptor);named = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) not in {0o600, 0o640} or before.st_size > 65536
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError('code_provider_delegation_file_not_root_controlled')
        raw = os.read(descriptor, 65537);after = os.fstat(descriptor)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != before.st_size):
            raise ValueError('code_provider_delegation_changed_during_read')
    finally:
        os.close(descriptor)
    value = loads(raw, max_bytes=65536, max_depth=16)
    if (not isinstance(value, dict) or set(value) != {'schema', 'enabled', 'delegations'}
            or value.get('schema') != DELEGATION_SCHEMA or value.get('enabled') is not True
            or not isinstance(value.get('delegations'), list) or not 1 <= len(value['delegations']) <= 32):
        raise ValueError('code_provider_delegation_contract_invalid')
    seen = set()
    for entry in value['delegations']:
        _validate_entry(entry)
        owner = _digest(entry['owner_scope'])
        if owner in seen:
            raise ValueError('code_provider_delegation_ambiguous')
        seen.add(owner)
    return value


def _validate_entry(entry: Any) -> None:
    if (not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS
            or entry.get('capability_id') != CAPABILITY_ID or entry.get('capability_scope') != 'global'
            or entry.get('operation') != OPERATION or not isinstance(entry.get('profile_key'), str)
            or not entry['profile_key'] or entry['profile_key'] != entry['profile_key'].strip()
            or not _hex64(entry.get('profile_digest'))):
        raise ValueError('code_provider_delegation_entry_invalid')
    owner = _scope(entry['owner_scope']);provider = _scope(entry['provider_scope'])
    if (owner == provider or owner['tenant_id'] != provider['tenant_id']
            or owner['agent_id'] != provider['agent_id']):
        raise ValueError('code_provider_delegation_owner_mismatch')


def _basis(owner: dict, provider: dict, profile_key: str, profile_digest: str) -> tuple[str, str]:
    if provider == owner:
        return 'exact_scope', ''
    from eimemory.governance.l5.l5_scope_authority import authorized_capability_scopes
    if ScopeRef.from_dict(provider) in authorized_capability_scopes(ScopeRef.from_dict(owner)):
        return 'existing_product_scope', ''
    value = _secure_delegations()
    if value is not None:
        for entry in value['delegations']:
            if entry['owner_scope'] == owner and entry['provider_scope'] == provider:
                if entry['profile_key'] != profile_key or entry['profile_digest'] != profile_digest:
                    raise ValueError('code_provider_delegation_profile_mismatch')
                return 'root_code_provider_delegation', _digest(entry)
    raise ValueError('code_provider_scope_not_authorized')


def _profile(runtime: Any, provider: dict, profile_key: str) -> str:
    from eimemory.capabilities.profiles import CapabilityProfiles
    resolved = CapabilityProfiles(runtime.store).resolve(profile_key, runtime_scope=provider, capability_scope='global')
    requirements = resolved.get('requirements')
    if (not isinstance(requirements, list) or len(requirements) != 1
            or requirements[0].get('capability_id') != CAPABILITY_ID
            or not _hex64(resolved.get('profile', {}).get('profile_digest'))):
        raise ValueError('code_provider_profile_requires_exact_code_implementation')
    return resolved['profile']['profile_digest']


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
    if (basis not in {'exact_scope', 'existing_product_scope', 'root_code_provider_delegation'}
            or (basis == 'exact_scope') != (owner == provider)
            or (not _hex64(delegation) if basis == 'root_code_provider_delegation' else delegation != '')
            or snapshot.get('digest') != _digest({key: value for key, value in snapshot.items() if key != 'digest'})):
        raise ValueError('code_provider_authority_digest_invalid')
    return dict(snapshot)


def validate_provider_authority_snapshot(snapshot: Any, *, owner_scope: ScopeRef | Mapping[str, Any],
                                         profile_key: str = '') -> dict:
    value = validate_provider_authority(snapshot, owner_scope=owner_scope)
    if profile_key and value['profile_key'] != profile_key:
        raise ValueError('code_provider_authority_profile_key_mismatch')
    return value


def select_provider_authority(runtime: Any, *, owner_scope: ScopeRef | Mapping[str, Any], profile_key: str) -> dict:
    owner = _scope(owner_scope);provider = configured_provider_scope(owner)
    # Select only the configured product target. Missing delegation does not
    # search another registry or change ownership to make a missing route pass.
    if provider != owner:
        from eimemory.governance.l5.l5_scope_authority import authorized_capability_scopes
        permitted = ScopeRef.from_dict(provider) in authorized_capability_scopes(ScopeRef.from_dict(owner))
        delegated = _secure_delegations() if not permitted else None
        if not permitted and not (delegated and any(entry['owner_scope'] == owner and entry['provider_scope'] == provider
                                                    for entry in delegated['delegations'])):
            provider = owner
    profile_digest = _profile(runtime, provider, profile_key)
    basis, delegation_digest = _basis(owner, provider, profile_key, profile_digest)
    material = dict(schema=SCHEMA, owner_scope=owner, provider_scope=provider, capability_id=CAPABILITY_ID,
        capability_scope='global', operation=OPERATION, profile_key=profile_key, profile_digest=profile_digest,
        authorization_basis=basis, delegation_digest=delegation_digest)
    return {**material, 'digest': _digest(material)}


def verify_provider_authority(runtime: Any, snapshot: Any, *, owner_scope: ScopeRef | Mapping[str, Any],
                               profile_key: str = '') -> dict:
    value = validate_provider_authority(snapshot, owner_scope=owner_scope)
    if profile_key and value['profile_key'] != profile_key:
        raise ValueError('code_provider_authority_profile_key_mismatch')
    actual = select_provider_authority(runtime, owner_scope=owner_scope, profile_key=value['profile_key'])
    if actual != value:
        raise ValueError('code_provider_authority_changed')
    return value


def delegated_provider_authority(runtime: Any, *, owner_scope: ScopeRef | Mapping[str, Any],
                                  profile_key: str) -> dict | None:
    """Optional explicit code-only route for the native assessment consumer."""
    value = _secure_delegations()
    if value is None:
        return None
    owner = _scope(owner_scope);provider = configured_provider_scope(owner)
    if provider == owner:
        return None
    if not any(entry['owner_scope'] == owner and entry['provider_scope'] == provider
                                for entry in value['delegations']):
        return None
    return select_provider_authority(runtime, owner_scope=owner, profile_key=profile_key)


def resolve_provider_authority(runtime: Any, *, owner_scope: ScopeRef | Mapping[str, Any], checked_at: str,
                               probe: bool = True, snapshot: Any = None, capability_scope: str = 'global') -> dict:
    from eimemory.adapters.hermes.code_implementation import resolve_code_implementation_provider
    owner = _scope(owner_scope)
    if snapshot is None:
        # No implicit delegation on old proposals, even after root maps change.
        return resolve_code_implementation_provider(runtime, runtime_scope=owner, capability_scope=capability_scope,
            checked_at=checked_at, probe=probe)
    if capability_scope != 'global':
        raise ValueError('code_provider_authority_capability_scope_invalid')
    value = verify_provider_authority(runtime, snapshot, owner_scope=owner)
    result = resolve_code_implementation_provider(runtime, runtime_scope=value['provider_scope'], capability_scope='global',
        checked_at=checked_at, probe=probe)
    return {**result, SOURCE_KEY: value}


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
