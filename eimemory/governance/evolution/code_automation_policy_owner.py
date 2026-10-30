"""Default-deny native delegation from a root standing grant to one incident.

This owner signs no provenance facts. The grant authorizes narrowly bounded
repair; detector origin, one-shot consumption and terminal/8h evidence retain
their independent native contracts. No grant is shipped or created here.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
import json
import os
import re
import stat
import subprocess

from eimemory.core.strict_json import loads
from eimemory.governance.evolution import code_automation_policy as policy_api
from eimemory.governance.evolution.code_automation_policy_capture import _private_json, verify_current_closure_capture
from eimemory.governance.evolution.code_automation_policy_incidents import (
    _ROUTES, _resolve_repo_root, select_current_system_incidents,
)
from eimemory.governance.evolution.code_automation_policy_issue import (
    _EFFECTS_BY_MODE, _git, _install_policy_atomically, _policy_lock, issue_code_automation_policy,
)
from eimemory.governance.evolution.code_evolution_repository import remote_url_digest
from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
from eimemory.governance.release.closure_verdict import report_digest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.atomic_file import atomic_write_json
from eimemory.storage.code_evolution_store import CodeEvolutionStore


STANDING_AUTHORITY_DIRECTORY = Path('/etc/eimemory-code-automation-authority')
from eimemory.contracts.user_control_plane import user_mode, coordinates, read_file as read_user_authority
if user_mode():
    STANDING_AUTHORITY_DIRECTORY = coordinates()['authority']/'standing'
SOURCE = 'eimemory.code_automation_policy_owner'
# One finite recovery generation; an unavailable machine never acquires an
# unbounded stream of renewed child policies for the same incident/release.
MAX_EXPIRY_RECOVERIES = 1
_FIELDS = {'schema', 'grant_id', 'enabled', 'not_before', 'expires_at', 'scope',
           'repository', 'profile_key', 'profile_digest', 'routes', 'effects', 'max_transactions'}


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError('standing_grant_time_invalid')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('standing_grant_time_invalid')
    return parsed.astimezone(timezone.utc)


def _scope_key(scope: ScopeRef | Mapping[str, Any]) -> str:
    value = asdict(scope) if isinstance(scope, ScopeRef) else dict(scope)
    if (set(value) != {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
            or any(not isinstance(v, str) or not v or v != v.strip() for v in value.values())):
        raise ValueError('standing_grant_exact_scope_required')
    return report_digest(value)


def standing_grant_path(scope: ScopeRef | Mapping[str, Any]) -> Path:
    # One exact path, no directory scan, wildcard or cross-scope fallback.
    return STANDING_AUTHORITY_DIRECTORY / ('scope-' + _scope_key(scope) + '.v1.json')


def standing_revocation_path(scope: ScopeRef | Mapping[str, Any]) -> Path:
    return STANDING_AUTHORITY_DIRECTORY / ('scope-' + _scope_key(scope) + '.revoked')


def _secure_read_standing_grant(scope: ScopeRef | Mapping[str, Any]) -> dict:
    path = standing_grant_path(scope)
    if policy_api._v2_no_symlink(path, missing_ok=False):
        raise ValueError('standing_grant_missing_or_unsafe')
    if os.name != 'posix' or not hasattr(os, 'geteuid'):
        raise ValueError('standing_grant_owner_check_unsupported')
    if user_mode():
        raw = read_user_authority(path)
    else:
        # Unlike /etc/eimemory, every authority directory is root controlled.
        for directory in (path.parent, *path.parent.parents):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or (stat.S_IMODE(info.st_mode) & 0o022
                        and not (directory != path.parent and info.st_mode & stat.S_ISVTX))):
                raise ValueError('standing_grant_parent_not_root_controlled')
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            named = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) not in {0o600, 0o640}
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                    or info.st_size > 65536):
                raise ValueError('standing_grant_file_not_root_controlled')
            raw = os.read(descriptor, 65537)
            after = os.fstat(descriptor)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != info.st_size:
                raise ValueError('standing_grant_changed_during_read')
        finally:
            os.close(descriptor)
    value = loads(raw, max_bytes=65536, max_depth=16)
    if not isinstance(value, dict):
        raise ValueError('standing_grant_object_required')
    return value


def _grant(scope: ScopeRef, root: Path, now: datetime) -> dict:
    grant = _secure_read_standing_grant(scope)
    return _validate_grant(grant, scope, root, now)


def _validate_grant(grant: dict, scope: ScopeRef, root: Path, now: datetime, *, git_runner: Any = None) -> dict:
    if (set(grant) not in (_FIELDS, _FIELDS | {'capability_authority'}) or grant.get('schema') != 'code_automation_standing_grant.v1'
            or grant.get('enabled') is not True or not isinstance(grant.get('grant_id'), str)
            or not 1 <= len(grant['grant_id']) <= 200 or grant.get('scope') != asdict(scope)
            or not isinstance(grant.get('profile_key'), str) or not grant['profile_key']
            or not isinstance(grant.get('profile_digest'), str) or len(grant['profile_digest']) != 64
            or any(c not in '0123456789abcdef' for c in grant['profile_digest'])
            or type(grant.get('max_transactions')) is not int or grant['max_transactions'] != 1
            or grant.get('effects') != _EFFECTS_BY_MODE['full']
            or any(type(v) is not bool for v in grant.get('effects', {}).values())):
        raise ValueError('standing_grant_contract_invalid')
    if 'capability_authority' in grant:
        from eimemory.governance.evolution.code_evolution_provider_authority import validate_provider_authority_snapshot
        authority = validate_provider_authority_snapshot(grant['capability_authority'], owner_scope=scope,
                                                         profile_key=grant['profile_key'])
        if authority['profile_digest'] != grant['profile_digest']:
            raise ValueError('standing_grant_capability_profile_digest_mismatch')
    start = _time(grant['not_before'])
    end = _time(grant['expires_at']) if grant['expires_at'] is not None else None
    if not start <= now or end is not None and not now < end:
        raise ValueError('standing_grant_expired_or_not_yet_valid')
    revoked = STANDING_AUTHORITY_DIRECTORY / 'standing-grant.revoked'
    switch = Path(os.environ.get(policy_api.CODE_EVOLUTION_KILL_SWITCH_ENV)
                  or policy_api.CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH)
    if (os.path.lexists(revoked) or os.path.lexists(standing_revocation_path(scope))
            or os.path.lexists(switch)):
        raise ValueError('standing_grant_revoked_or_killed')
    from eimemory.config.trusted import trusted_repository_root, trusted_remote, trusted_branch_allowed
    git_read = git_runner or _git
    remote = trusted_remote()
    try:
        branch = git_read(root, 'symbolic-ref', '--short', 'HEAD')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # Detached HEAD cannot satisfy grant.repository.branch; classify instead of
        # bubbling CalledProcessError into authority_check_unclassified.
        raise ValueError('standing_grant_detached_head') from exc
    actual = {'root': str(root), 'remote': remote, 'branch': branch,
              'remote_url_digest': remote_url_digest(git_read(root, 'remote', 'get-url', remote))}
    if (root != Path(trusted_repository_root()).resolve() or not trusted_branch_allowed(branch)
            or grant.get('repository') != actual):
        raise ValueError('standing_grant_repository_mismatch')
    routes = grant.get('routes')
    if not isinstance(routes, dict) or not routes or len(routes) > len(_ROUTES):
        raise ValueError('standing_grant_routes_invalid')
    for kind, route in routes.items():
        expected = _ROUTES.get(kind)
        if (expected is None or not isinstance(route, dict)
                or route != {'source': expected[0], 'detector_id': expected[0]+'.v1',
                             'test_plan_id': expected[1],
                             'test_plan_digest': protected_test_plan_digest(expected[1])}):
            raise ValueError('standing_grant_route_mismatch')
    return grant


def _verify_profile(runtime: Any, scope: ScopeRef, grant: dict) -> None:
    from eimemory.capabilities.profiles import CapabilityProfiles
    selector = scope
    if 'capability_authority' in grant:
        from eimemory.governance.evolution.code_evolution_provider_authority import verify_provider_authority
        authority = verify_provider_authority(runtime, grant['capability_authority'], owner_scope=scope,
                                               profile_key=grant['profile_key'])
        selector = ScopeRef.from_dict(authority['provider_scope'])
    resolved = CapabilityProfiles(runtime.store).resolve(grant['profile_key'], runtime_scope=selector, capability_scope='global')
    if resolved.get('profile', {}).get('profile_digest') != grant['profile_digest']:
        raise ValueError('standing_grant_profile_digest_mismatch')


def verify_native_standing_authority(runtime: Any, *, transaction: Mapping[str, Any],
                                    repo_root: str | Path | None = None) -> dict:
    """Read-only fresh admission for consumed native children, never rollback.

    This does not issue/extend authority, consume a policy, retire an incident,
    construct a Runtime, or claim that the transaction qualifies for L5.
    Manual policy authority retains its separate original effect contract.
    """
    payload = transaction.get('payload')
    authorized = payload.get('authorized_policy') if isinstance(payload, Mapping) else None
    policy_id = authorized.get('policy_id', '') if isinstance(authorized, Mapping) else ''
    if not isinstance(policy_id, str) or not policy_id.startswith('native-incident-'):
        return {'ok': True, 'status': 'not_native'}
    root = Path(_resolve_repo_root(repo_root)).expanduser().resolve()
    state = Path(runtime.store.root) / 'state' / 'code-automation-owner'
    if policy_api._v2_no_symlink(state / 'published.json', missing_ok=False):
        raise ValueError('native_transaction_intent_unverified')
    binding = _private_json(state / 'published.json')
    key = binding.get('intent_key')
    if not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}(?:-generation-1)?', key) is None:
        raise ValueError('native_transaction_intent_unverified')
    intent = _private_json(state / (key + '.json'))
    original = loads(intent['policy_bytes'], max_bytes=65536, max_depth=32)
    scope = _prior_owner_scope(intent)
    digest = policy_api._policy_digest(original)
    if (binding.get('policy_digest') != digest or intent.get('policy_digest') != digest
            or transaction.get('policy_digest') != digest or authorized.get('policy_digest') != digest
            or original.get('policy_id') != policy_id or intent.get('intent_key') != key
            or transaction.get('repository_root') != str(root)
            or any(transaction.get(k) != v for k, v in asdict(scope).items())
            or transaction.get('base_commit') != original.get('repository', {}).get('base_commit')
            or transaction.get('incident_id') != intent.get('incident_record_id')):
        raise ValueError('native_transaction_intent_mismatch')
    grant = _grant(scope, root, datetime.now(timezone.utc))
    if (report_digest(grant) != intent.get('grant_digest')
            or grant.get('capability_authority') != intent.get('capability_authority')):
        raise ValueError('native_transaction_standing_authority_changed')
    _verify_profile(runtime, scope, grant)
    return {'ok': True, 'status': 'verified', 'grant_digest': report_digest(grant),
            'policy_digest': digest, 'intent_key': key}


def _selection(runtime: Any, scope: ScopeRef, root: Path) -> dict:
    selected = select_current_system_incidents(runtime, scope=scope, root=root)
    if selected.get('status') == 'current' and not getattr(selected['release'], 'complete', False):
        raise ValueError('current_release_authority_incomplete')
    return selected


def _source_fence(runtime: Any, selected: dict, scope: ScopeRef, record: Any, incident: dict) -> dict:
    if incident['incident_class'].startswith('release.closure_'):
        return verify_current_closure_capture(runtime, scope=scope, release=selected['release'],
                                              record=record, incident=incident)
    # The shared selector revalidates native runtime current-observation evidence.
    if incident['incident_class'] == 'deployment.runtime_commit_drift':
        return {'incident_digest': incident['incident_digest']}
    if incident['incident_class'] == 'code.system_repair_policy_stale':
        from eimemory.ops.system_code_repair_failure import current_system_code_repair_failure
        if current_system_code_repair_failure(runtime.store, scope=scope,
                expected_commit=selected['release'].commit) != incident['incident_digest']:
            raise ValueError('repair_current_observation_unverified')
        return {'incident_digest': incident['incident_digest']}
    # A router failure has no native current-success retirement fence yet.
    # Keep manual recovery available, but never derive automatic new authority.
    raise ValueError('incident_current_authority_not_supported')


def _policy_at(path: Path) -> tuple[dict | None, str]:
    if not os.path.lexists(path):
        return None, ''
    raw, error = policy_api._secure_read_v2_policy(path)
    if error:
        raise ValueError('existing_policy_unverified:' + error)
    policy = loads(raw, max_bytes=65536, max_depth=32)
    if not isinstance(policy, dict):
        raise ValueError('existing_policy_invalid')
    return policy, policy_api._policy_digest(policy)


def ensure_native_incident_policy(runtime: Any, *, scope: ScopeRef | Mapping[str, Any],
                                  repo_root: str | Path | None = None, apply: bool = True) -> dict:
    """Issue once from genuine current evidence; dry runs never write authority."""
    if apply is not True:
        return {'ok': True, 'status': 'skipped', 'reason': 'apply_disabled', 'repair_complete': False}
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    root = Path(_resolve_repo_root(repo_root)).expanduser().resolve()
    try:
        selected = _selection(runtime, scope_ref, root)
        if selected.get('status') != 'current':
            result = {k: v for k, v in selected.items() if k not in {'current', 'release'}}
            if selected.get('status') == 'idle':
                # An empty incident queue does not validate installed standing
                # authority. In particular, a protected test-plan change can
                # leave every future repair inadmissible while ticks say idle.
                # Optional authority remains optional; never install or renew it.
                if os.path.lexists(standing_grant_path(scope_ref)):
                    grant = _grant(scope_ref, root, datetime.now(timezone.utc))
                    _verify_profile(runtime, scope_ref, grant)
                    result['standing_authority'] = {
                        'status': 'verified', 'grant_digest': report_digest(grant),
                        'effects_exercised': False,
                    }
                else:
                    result['standing_authority'] = {
                        'status': 'not_configured', 'effects_exercised': False,
                    }
            return result
        state = Path(runtime.store.root) / 'state' / 'code-automation-owner'
        destination = Path(policy_api.CODE_AUTOMATION_POLICY_DEFAULT_PATH)
        current_policy, current_digest = _policy_at(destination)
        # An unowned manual policy is never replaced. The router validates it.
        owned = state / 'published.json'
        binding = _private_json(owned) if owned.exists() else None
        if current_policy is not None and (not binding or binding.get('policy_digest') != current_digest):
            # A crash may have published the exact current intent before receipt.
            matching_intent = False
            for _, incident in selected['current']:
                key = _intent_key(scope_ref, selected['release'], incident)
                for candidate_key in (key, key + '-generation-1'):
                    path = state / (candidate_key + '.json')
                    if path.exists() and _private_json(path).get('policy_digest') == current_digest:
                        matching_intent = True
                        break
            if not matching_intent:
                return {'ok': True, 'status': 'manual_policy_preserved', 'repair_complete': False}
        now = datetime.now(timezone.utc)
        grant = _grant(scope_ref, root, now)
        candidates = [(r, i) for r, i in selected['current'] if i['incident_class'] in grant['routes']]
        if not candidates:
            raise ValueError('standing_grant_incident_not_authorized')
        # Selection is deterministic within the bounded, verified source page.
        record, incident = sorted(candidates, key=lambda pair: pair[1]['incident_digest'])[0]
        source = _source_fence(runtime, selected, scope_ref, record, incident)
        _verify_profile(runtime, scope_ref, grant)
        from eimemory.adapters.hermes.code_implementation import resolve_code_implementation_provider
        if 'capability_authority' in grant:
            from eimemory.governance.evolution.code_evolution_provider_authority import resolve_provider_authority
            provider = resolve_provider_authority(runtime, owner_scope=scope_ref, checked_at=now.isoformat(),
                                                 probe=True, snapshot=grant['capability_authority'])
        else:
            # Existing authority remains exact-owner even when a new root
            # provider delegation is installed later.
            provider = resolve_code_implementation_provider(runtime, runtime_scope=asdict(scope_ref), capability_scope='global',
                        checked_at=now.isoformat(), probe=True)
        if (provider.get('ok') is not True or provider.get('provider_ready') is not True
                or provider.get('advertisement_fresh') is not True or provider.get('health', {}).get('ok') is not True):
            raise ValueError('native_provider_unavailable')
        # No grant/incident/provider -> no owner directory, intent or policy write.
        if policy_api._v2_no_symlink(state / '.path-check', missing_ok=True):
            raise ValueError('policy_owner_state_path_invalid')
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state.stat()
        if os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('policy_owner_state_not_private')
        with _policy_lock(state / 'owner.lock'):
            return _issue_locked(runtime, scope_ref, root, selected, record, incident, source,
                                 grant, provider, state, destination)
    except Exception as exc:
        return {'ok': False, 'status': 'blocked', 'reason': str(exc) if isinstance(exc, ValueError)
                else 'native_policy_owner_error:' + type(exc).__name__, 'repair_complete': False}


def _intent_key(scope: ScopeRef, release: Any, incident: dict) -> str:
    # No grant/clock in the key: renewal cannot reset one-shot consumption.
    return report_digest({'scope': asdict(scope), 'commit': release.commit,
                          'receipt_id': release.receipt_id, 'session_id': release.session_id,
                          'incident_digest': incident['incident_digest']})


def _prior_owner_scope(intent: dict) -> ScopeRef:
    raw = intent.get('scope')
    _scope_key(raw)
    exact = ScopeRef.from_dict(raw)
    if asdict(exact) != raw:
        raise ValueError('prior_policy_owner_unverified')
    return exact


def _verify_prior_unconsumed_retired(runtime: Any, root: Path, intent: dict, policy: dict) -> None:
    """A different new caller cannot retire the old owner's live authority."""
    from eimemory.governance.evolution.code_automation_policy_incidents import _trusted_incident_identity
    from eimemory.governance.release.evidence_contract import current_release_identity
    previous_scope = _prior_owner_scope(intent)
    record = runtime.store.get_by_id(intent.get('incident_record_id', ''))
    incident = _trusted_incident_identity(record, policy.get('repository', {}).get('base_commit', '')) if record else None
    if (not record or record.scope != previous_scope or not incident
            or incident['incident_digest'] != intent.get('incident_digest')
            or incident['incident_digest'] != policy.get('incident', {}).get('incident_digest')
            or incident['incident_class'] != policy.get('incident', {}).get('class')):
        raise ValueError('prior_policy_source_unverified')
    release = current_release_identity(runtime, previous_scope)
    if release is None or not release.complete:
        raise ValueError('prior_policy_currentness_unverified')
    previous = _selection(runtime, previous_scope, root)
    if previous.get('ok') is not True or previous.get('status') not in {'idle', 'current'}:
        raise ValueError('prior_policy_currentness_unverified')
    matches = [(r, i) for r, i in previous.get('current', []) if i['incident_digest'] == incident['incident_digest']]
    if matches:
        try:
            same_source = len(matches) == 1 and _source_fence(runtime, previous, previous_scope, *matches[0]) == intent.get('source')
        except (ValueError, OSError) as exc:
            raise ValueError('prior_policy_currentness_unverified') from exc
        if not same_source:
            raise ValueError('prior_policy_currentness_unverified')
        raise ValueError('prior_policy_incident_still_current')
    if incident['incident_class'].startswith('release.closure_'):
        from eimemory.governance.evolution.code_automation_policy_capture import _current_snapshot
        _, capture, result, actual = _current_snapshot(runtime, scope=previous_scope, release=release)
        # Only the prior policy's own capture/incident may block retirement.
        # A sticky failure_detected for a *different* incident that selection
        # already ops-dropped (projection lag / oneshot quiet) must not freeze
        # an unrelated unconsumed prior — that deadlock left Hermes npo blocked
        # with prior_policy_currentness_unverified after 1.15.65/1.15.67 drops.
        if (capture['capture_id'] == intent.get('source', {}).get('capture_id')
                or result.get('incident_record_id') == intent.get('incident_record_id')):
            raise ValueError('prior_policy_currentness_unverified')
    elif incident['incident_class'] == 'deployment.runtime_commit_drift':
        from eimemory.ops.runtime_identity_drift import current_runtime_identity_incident
        current = current_runtime_identity_incident(runtime.store, scope=previous_scope, expected_commit=release.commit)
        if current is None or current == incident['incident_digest']:
            raise ValueError('prior_policy_currentness_unverified')
    elif incident['incident_class'] == 'code.system_repair_policy_stale':
        from eimemory.ops.system_code_repair_failure import current_system_code_repair_failure
        current = current_system_code_repair_failure(runtime.store, scope=previous_scope, expected_commit=release.commit)
        if current is None or current == incident['incident_digest']:
            raise ValueError('prior_policy_currentness_unverified')
    else:
        raise ValueError('prior_policy_currentness_unverified')


def _verify_policy_replacement(runtime: Any, ledger: Any, root: Path, state: Path,
                               destination: Path, policy: dict | None, digest: str,
                               replacement_key: str = '') -> None:
    if ledger.repository_blocker(str(root)):
        raise ValueError('repository_transaction_active')
    if policy is None:
        return
    binding_path = state / 'published.json'
    binding = _private_json(binding_path) if binding_path.exists() else {}
    if binding.get('policy_digest') != digest:
        raise ValueError('manual_policy_present')
    old = _private_json(state / (binding['intent_key'] + '.json'))
    if old.get('policy_digest') != digest:
        raise ValueError('prior_owned_policy_unverified')
    raw, read_error = policy_api._secure_read_v2_policy(destination)
    if read_error or raw != old.get('policy_bytes'):
        raise ValueError('prior_owned_policy_bytes_changed')
    consumed = ledger.get_policy_consumption(digest)
    if not consumed:
        retirement_path = state / (old['intent_key'] + '-retirement.json')
        if retirement_path.exists():
            _verified_retirement(retirement_path, old, replacement_key)
            _verify_no_incident_transaction(ledger, root, old)
            return
        _verify_prior_unconsumed_retired(runtime, root, old, policy)
        return
    transaction = ledger.get_transaction(str(consumed.get('transaction_id') or ''))
    if (not transaction or type(transaction.get('terminal')) not in {bool, int} or transaction['terminal'] != 1):
        raise ValueError('prior_policy_transaction_incomplete')
    terminal = ledger.get_terminal_receipt(transaction['transaction_id'])
    outcomes = {'SUCCEEDED_SEDIMENTED': 'succeeded_sedimented', 'ROLLED_BACK_HEALTHY': 'rolled_back_healthy',
                'ABORTED_NO_EXTERNAL_EFFECT': 'aborted_no_external_effect',
                'ABORTED_CANDIDATE_RESTORED': 'aborted_candidate_restored', 'RECOVERY_QUARANTINED': 'recovery_quarantined'}
    previous_scope = asdict(_prior_owner_scope(old))
    if (any(transaction.get(k) != v for k, v in previous_scope.items())
            or transaction.get('payload', {}).get('scope') != previous_scope
            or not terminal or terminal.get('outcome') != outcomes.get(transaction.get('current_state'))
            or terminal.get('receipt_digest') != transaction.get('terminal_receipt_digest')
            or transaction.get('policy_digest') != digest
            or transaction.get('authorization_digest') != consumed.get('authorization_receipt_digest')):
        raise ValueError('prior_policy_terminal_receipt_unverified')


def _verified_retirement(path: Path, intent: dict, next_key: str) -> dict:
    retired = _private_json(path)
    policy = loads(intent['policy_bytes'], max_bytes=65536, max_depth=32)
    fields = {'schema', 'intent_key', 'policy_digest', 'intent_digest', 'reason', 'retired_at',
              'next_intent_key', 'generation', 'terminal_evidence', 'digest'}
    if (set(retired) != fields or retired.get('schema') != 'code_automation_policy_retirement.v1'
            or retired['digest'] != report_digest({k: v for k, v in retired.items() if k != 'digest'})
            or retired['policy_digest'] != intent['policy_digest'] or retired['intent_key'] != intent['intent_key']
            or retired['intent_digest'] != report_digest(intent) or retired['next_intent_key'] != next_key
            or retired['reason'] != 'expired_unconsumed_no_transaction' or retired['terminal_evidence'] is not False
            or type(retired['generation']) is not int or retired['generation'] != 0
            or not _time(policy['expires_at']) <= _time(retired['retired_at']) <= datetime.now(timezone.utc)):
        raise ValueError('prior_policy_retirement_unverified')
    return retired


def _verify_no_incident_transaction(ledger: Any, root: Path, intent: dict) -> None:
    """Never reset one-shot execution, including a proposal not yet consumed."""
    policy = loads(intent['policy_bytes'], max_bytes=65536, max_depth=32)
    if ledger.find_incident_transaction(scope=asdict(_prior_owner_scope(intent)), repository_root=str(root),
            incident_id=intent['incident_record_id'], incident_digest=intent['incident_digest'],
            base_commit=policy['repository']['base_commit']) is not None:
        raise ValueError('incident_already_has_transaction')
    if ledger.get_policy_consumption(intent['policy_digest']):
        raise ValueError('incident_policy_already_consumed')


def _expiry_generation(runtime: Any, ledger: Any, root: Path, state: Path, key: str) -> tuple[str, int]:
    """Append retirement evidence; retain every original intent byte and clock."""
    now = datetime.now(timezone.utc)
    for generation in range(MAX_EXPIRY_RECOVERIES + 1):
        candidate = key if generation == 0 else key + '-generation-' + str(generation)
        path = state / (candidate + '.json')
        if not path.exists():
            if generation == 0 and ((state / (key + '-generation-1.json')).exists()
                                    or (state / (key + '-retirement.json')).exists()):
                raise ValueError('policy_generation_history_missing')
            return candidate, generation
        intent = _private_json(path)
        policy = loads(intent['policy_bytes'], max_bytes=65536, max_depth=32)
        if (intent.get('intent_key') != candidate or policy_api._policy_digest(policy) != intent.get('policy_digest')):
            raise ValueError('existing_intent_policy_changed')
        if ledger.get_policy_consumption(intent['policy_digest']) or now < _time(policy['expires_at']):
            return candidate, generation
        _verify_no_incident_transaction(ledger, root, intent)
        if generation == MAX_EXPIRY_RECOVERIES:
            raise ValueError('incident_authority_generations_exhausted')
        next_key = key + '-generation-' + str(generation + 1)
        retirement_path = state / (candidate + '-retirement.json')
        if retirement_path.exists():
            retired = _verified_retirement(retirement_path, intent, next_key)
        else:
            retired = {'schema': 'code_automation_policy_retirement.v1', 'intent_key': candidate,
                'policy_digest': intent['policy_digest'], 'intent_digest': report_digest(intent),
                'reason': 'expired_unconsumed_no_transaction', 'retired_at': now.isoformat(),
                'next_intent_key': next_key, 'generation': generation, 'terminal_evidence': False}
            retired['digest'] = report_digest(retired)
            atomic_write_json(retirement_path, retired)
        record = RecordEnvelope.create(kind='reflection', title='Expired native policy retired without execution',
            scope=_prior_owner_scope(intent), source=SOURCE, status='active', content=retired,
            meta={'idempotency_key': 'native-policy-retirement:' + candidate})
        runtime.store.append(record, existing_match=lambda row: row.source == SOURCE and row.content == retired)
    raise ValueError('incident_authority_generations_exhausted')


def _issue_locked(runtime: Any, scope: ScopeRef, root: Path, selected: dict, record: Any,
                  incident: dict, source: dict, grant: dict, provider: dict,
                  state: Path, destination: Path) -> dict:
    ledger = CodeEvolutionStore(runtime.store)
    family_key = _intent_key(scope, selected['release'], incident)
    key, generation = _expiry_generation(runtime, ledger, root, state, family_key)
    path = state / (key + '.json')
    current_policy, current_digest = _policy_at(destination)
    if path.exists():
        intent = _private_json(path)
        if (intent.get('schema') != 'code_automation_policy_intent.v1' or intent.get('intent_key') != key
                or intent.get('grant_digest') != report_digest(grant) or intent.get('source') != source
                or intent.get('scope') != asdict(scope) or intent.get('incident_digest') != incident['incident_digest']
                or intent.get('capability_authority') != grant.get('capability_authority')):
            raise ValueError('existing_intent_authority_changed')
        policy = loads(intent['policy_bytes'], max_bytes=65536, max_depth=32)
        if policy_api._policy_digest(policy) != intent['policy_digest']:
            raise ValueError('existing_intent_policy_changed')
    else:
        _verify_policy_replacement(runtime, ledger, root, state, destination, current_policy, current_digest, key)
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=2)
        if grant['expires_at'] is not None:
            end = min(end, _time(grant['expires_at']))
        route = grant['routes'][incident['incident_class']]
        issued = issue_code_automation_policy(repo_root=root, incident_class=incident['incident_class'],
            detector_id=route['detector_id'], test_plan_id=route['test_plan_id'], effects_mode='full',
            policy_id='native-incident-' + family_key[:32] + (('-g' + str(generation)) if generation else ''),
            profile_key=grant['profile_key'], max_transactions=1,
            incident_digest=incident['incident_digest'], not_before=now.isoformat(), expires_at=end.isoformat())
        if issued.get('ok') is not True:
            raise ValueError('native_issuer_rejected:' + str(issued.get('reason')))
        policy = issued['policy']
        if (policy['capability']['implementation_digest'] != provider.get('implementation_digest')
                or policy['repository']['base_commit'] != selected['release'].commit):
            raise ValueError('native_issuer_live_binding_mismatch')
        intent = {'schema': 'code_automation_policy_intent.v1', 'intent_key': key,
                  'grant_digest': report_digest(grant), 'scope': asdict(scope),
                  'incident_record_id': record.record_id, 'incident_digest': incident['incident_digest'],
                  'source': source, 'policy_bytes': json.dumps(policy, indent=2)+'\n',
                  'policy_digest': policy_api._policy_digest(policy), 'expected_previous_digest': current_digest,
                  'created_at': now.isoformat(), 'provider': {k: provider[k] for k in (
                      'implementation_digest', 'advertisement_id', 'advertisement_digest',
                      'catalog_case_id', 'catalog_snapshot_digest', 'catalog_activation_state_digest')}}
        if generation:
            intent.update(family_intent_key=family_key, generation=generation)
        if 'capability_authority' in grant:
            intent['capability_authority'] = grant['capability_authority']
        atomic_write_json(path, intent)
    consumed = ledger.get_policy_consumption(intent['policy_digest'])
    if consumed:
        return {'ok': True, 'status': 'consumed', 'policy_digest': intent['policy_digest'],
                'transaction_id': consumed.get('transaction_id'), 'repair_complete': False,
                **({'capability_authority': intent['capability_authority']} if 'capability_authority' in intent else {})}
    # Late re-read closes grant, repository, release, and native source races.
    fresh_grant = _grant(scope, root, datetime.now(timezone.utc))
    _verify_profile(runtime, scope, fresh_grant)
    fresh = _selection(runtime, scope, root)
    matches = [(r, i) for r, i in fresh.get('current', []) if i['incident_digest'] == incident['incident_digest']]
    if (report_digest(fresh_grant) != intent['grant_digest'] or len(matches) != 1
            or _intent_key(scope, fresh['release'], matches[0][1]) != family_key
            or _source_fence(runtime, fresh, scope, *matches[0]) != intent['source']):
        raise ValueError('incident_or_grant_changed_before_publication')
    if not _time(policy['not_before']) <= datetime.now(timezone.utc) < _time(policy['expires_at']):
        raise ValueError('original_intent_policy_expired')
    fresh_policy, fresh_digest = _policy_at(destination)
    if fresh_digest != intent['policy_digest']:
        if fresh_digest != intent['expected_previous_digest']:
            raise ValueError('existing_policy_changed_before_publication')
        # Replays and a first publication both recheck the shared repository
        # and the original owner's lifecycle before replacing global authority.
        _verify_policy_replacement(runtime, ledger, root, state, destination, fresh_policy, fresh_digest, key)
        error = _install_policy_atomically(destination, policy, expected_digest=intent['expected_previous_digest'])
        if error:
            raise ValueError(error)
    raw, read_error = policy_api._secure_read_v2_policy(destination)
    if read_error or raw != intent['policy_bytes']:
        raise ValueError('published_policy_bytes_changed')
    # Publication may have succeeded before a crash/fsync error. Equal bytes
    # still need the durability boundary before a delegation can be reported.
    from eimemory.storage.atomic_file import _fsync_directory
    _fsync_directory(destination.parent)
    # The strict native loader validates the actual installed authority.
    loaded = policy_api.load_code_automation_policy(path=destination)
    if loaded.get('ok') is not True or loaded.get('policy_digest') != intent['policy_digest']:
        raise ValueError('published_policy_readback_unverified')
    receipt = {'schema': 'code_automation_policy_delegation.v1', 'intent_key': key,
               'policy_digest': intent['policy_digest'], 'grant_digest': intent['grant_digest'],
               'scope': asdict(scope), 'incident_record_id': record.record_id,
               'incident_digest': incident['incident_digest'], 'source': source,
               'policy_created_at': intent['created_at'], 'terminal_evidence': False, 'repair_complete': False,
               'l5_origin_verified': False,
               'source_facts': record.content.get('detector_report', {}).get('source_facts')}
    if 'capability_authority' in intent:
        receipt['capability_authority'] = intent['capability_authority']
    record_out = RecordEnvelope.create(kind='reflection', title='Native incident policy delegation',
        summary='Standing grant delegated to exact native incident; no repair or terminal success is claimed.',
        content=receipt, source=SOURCE, scope=scope, status='active',
        provenance={'origin': 'native_policy_owner'}, meta={'idempotency_key': 'native-policy-delegation:'+key})
    stored = runtime.store.append(record_out, existing_match=lambda r:
        r.kind == 'reflection' and r.source == SOURCE and r.scope == scope and r.content == receipt)
    if stored.content != receipt or stored.source != SOURCE or stored.scope != scope:
        raise ValueError('delegation_receipt_mismatch')
    atomic_write_json(state / 'published.json', {'intent_key': key, 'policy_digest': intent['policy_digest']})
    return {'ok': True, 'status': 'available', 'policy_digest': intent['policy_digest'],
            'delegation_record_id': stored.record_id, 'incident_record_id': record.record_id,
            'repair_complete': False,
            **({'capability_authority': intent['capability_authority']} if 'capability_authority' in intent else {})}
