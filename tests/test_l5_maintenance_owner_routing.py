from dataclasses import asdict
from pathlib import Path

import pytest

from eimemory.models.records import ScopeRef
from eimemory.governance.evolution import system_code_maintenance as maintenance
from eimemory.governance.evolution import code_automation_policy_owner as owner
from eimemory.governance.evolution import code_evolution_provider_authority as providers

_REAL_GRANT_READER = owner._secure_read_standing_grant

PROVIDER = dict(tenant_id='tenant', agent_id='agent', workspace_id='service', user_id='operator')
OWNER = dict(PROVIDER, workspace_id='channel', user_id='logical-owner')


@pytest.mark.parametrize('defect', ['', 'provider', 'tenant', 'grant', 'profile', 'revoked', 'entry_profile'])
def test_registered_owner_requires_own_current_authority(monkeypatch, defect):
    target = dict(OWNER)
    provider = dict(PROVIDER)
    if defect == 'provider':
        provider['user_id'] = 'other'
    if defect == 'tenant':
        target['tenant_id'] = 'other'
    monkeypatch.setattr(providers, '_secure_delegations', lambda: {'delegations': [
        dict(owner_scope=target, provider_scope=provider, profile_key='code', profile_digest='a' * 64)]})
    monkeypatch.setattr(owner, '_resolve_repo_root', lambda _: Path('/repo'))
    def grant(scope, root, now):
        assert asdict(scope) == OWNER
        if defect in ('grant', 'revoked'):
            raise ValueError('standing_grant_revoked_or_killed')
        return dict(profile_key='code', profile_digest=('b' if defect == 'entry_profile' else 'a') * 64)
    monkeypatch.setattr(owner, '_grant', grant)
    def profile(*args):
        if defect == 'profile':
            raise ValueError('standing_grant_profile_digest_mismatch')
    monkeypatch.setattr(owner, '_verify_profile', profile)
    calls = []
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda runtime, **kw:
                        calls.append(asdict(kw['scope']) if isinstance(kw['scope'], ScopeRef) else dict(kw['scope'])) or dict(ok=True, execution_ok=True, status='idle'))
    result = maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='watch', include_registered_owners=True)
    assert calls == ([PROVIDER, OWNER] if not defect else ([PROVIDER] if defect in ('provider', 'tenant') else []))
    assert result['ok'] is (defect not in ('grant', 'profile', 'revoked', 'entry_profile'))


def test_dry_run_does_not_read_registry(monkeypatch):
    monkeypatch.setattr(providers, '_secure_delegations', lambda: pytest.fail('dry run discovery'))
    assert maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='watch', apply=False, include_registered_owners=True)['status'] == 'skipped'


def test_invalid_registry_blocks_discovery_without_reading_incidents(monkeypatch):
    def invalid():
        raise ValueError('code_provider_delegation_contract_invalid')
    monkeypatch.setattr(providers, '_secure_delegations', invalid)
    calls = []
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda runtime, **kw:
                        calls.append(kw['scope']) or dict(ok=True, execution_ok=True, status='idle'))
    result = maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='watch', include_registered_owners=True)
    assert calls == []
    assert result['ok'] is False and result['owner_scope_reports'] == []


def test_explicit_single_scope_contract_does_not_expand(monkeypatch):
    monkeypatch.setattr(providers, '_secure_delegations', lambda: pytest.fail('single-scope discovery'))
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda *args, **kw: dict(ok=True))
    assert maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='explicit')['ok']


@pytest.mark.parametrize('failure', ['revoked', 'expired'])
def test_all_candidates_preflight_before_primary_or_owner_effects(monkeypatch, failure):
    second = dict(OWNER, user_id='second')
    monkeypatch.setattr(providers, '_secure_delegations', lambda: {'delegations': [
        dict(owner_scope=target, provider_scope=PROVIDER, profile_key='code', profile_digest='a'*64)
        for target in (OWNER, second)]})
    checked = []
    def grant(scope, *args):
        checked.append(asdict(scope))
        if asdict(scope) == second:
            raise ValueError(failure)
        return dict(profile_key='code', profile_digest='a'*64)
    monkeypatch.setattr(owner, '_grant', grant)
    monkeypatch.setattr(owner, '_verify_profile', lambda *args: None)
    monkeypatch.setattr(owner, '_resolve_repo_root', lambda _: Path('/repo'))
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda *args, **kw: pytest.fail('preflight must precede effects'))
    result = maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='watch', include_registered_owners=True)
    assert checked == [OWNER, second]
    assert result['ok'] is False and result['owner_scope_reports'] == []
    assert result['primary_report'] is None


def test_primary_failure_stops_registered_owners_and_preserves_result(monkeypatch):
    monkeypatch.setattr(providers, '_secure_delegations', lambda: {'delegations': [
        dict(owner_scope=OWNER, provider_scope=PROVIDER, profile_key='code', profile_digest='a'*64)]})
    events = []
    monkeypatch.setattr(owner, '_resolve_repo_root', lambda _: Path('/repo'))
    monkeypatch.setattr(owner, '_grant', lambda *args: events.append('grant') or dict(profile_key='code', profile_digest='a'*64))
    monkeypatch.setattr(owner, '_verify_profile', lambda *args: events.append('profile'))
    primary = dict(ok=False, execution_ok=False, status='blocked', reason='real_primary_failure', detector={'processed': 1})
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda *args, **kw: events.append(asdict(kw['scope'])) or primary)
    result = maintenance.run_system_code_maintenance(object(), scope=PROVIDER, owner_id='watch', include_registered_owners=True)
    assert events == ['grant', 'profile', PROVIDER]
    assert result['primary_report'] == primary
    assert result['reason'] == 'real_primary_failure'
    assert result['owner_scope_reports'] == []


from copy import deepcopy
import json
from native_policy_owner_cases import native_policy_fixture, _REAL_PROFILE_RESOLVER
from eimemory.capabilities import CapabilityProfile
from eimemory.capabilities.profiles import CapabilityProfiles
from datetime import datetime, timezone


@pytest.fixture
def routing_security_fixture(native_policy_fixture, monkeypatch):
    """Isolated file bytes; real registry schema, grant validation and SQLite profiles."""
    f = native_policy_fixture
    monkeypatch.setenv('EIMEMORY_CONTROL_PLANE', 'user')
    # Scratch ancestors are not enrolled authority directories. Substitute only
    # the machine ownership boundary; native JSON/schema and authorization stay real.
    monkeypatch.setattr(owner, 'read_user_authority', lambda path: path.read_bytes())
    monkeypatch.setattr(providers, 'read_user_authority', lambda path: path.read_bytes())
    # Restore the reader/profile resolver substituted by the existing native fixture.
    monkeypatch.setattr(CapabilityProfiles, 'resolve', _REAL_PROFILE_RESOLVER)
    from eimemory.capabilities import CapabilityDefinition
    from eimemory.capabilities.registry import CapabilityRegistry
    CapabilityRegistry(f.runtime.store).register_definition(CapabilityDefinition(
        capability_id='code.implementation', display_name='Fixture code', description='Isolated fixture',
        owner='fixture', risk_tier='bounded_read', tags=(), provenance={'source': 'test'},
        created_at=datetime.now(timezone.utc).isoformat(), scope='global'), runtime_scope=f.scope)
    CapabilityProfiles(f.runtime.store).register(CapabilityProfile(
        profile_id='isolated.fixture:routing', profile_key=f.grant['profile_key'], revision='routing', scope='global',
        requirements={'code.implementation': {'minimum_maturity': 'evaluated', 'min_pass_rate': 1.0}},
        provenance={'source': 'isolated-routing-test'}, created_at=datetime.now(timezone.utc).isoformat()),
        runtime_scope=f.scope)
    f.grant['profile_digest'] = CapabilityProfiles(f.runtime.store).resolve(
        f.grant['profile_key'], runtime_scope=f.scope, capability_scope='global')['profile']['profile_digest']
    f.provider_scope = ScopeRef(f.scope.tenant_id, f.scope.agent_id, 'service', 'operator')
    f.entry = dict(owner_scope=asdict(f.scope), provider_scope=asdict(f.provider_scope),
                   capability_id=providers.CAPABILITY_ID, capability_scope='global', operation=providers.OPERATION,
                   profile_key=f.grant['profile_key'], profile_digest=f.grant['profile_digest'])
    f.registry = dict(schema=providers.DELEGATION_SCHEMA, enabled=True, delegations=[f.entry])
    f.registry_path = f.root / 'registry.json'
    monkeypatch.setattr(providers, 'DELEGATION_PATH', f.registry_path)
    # Capture the native reader before the shared fixture replaces it.
    monkeypatch.setattr(owner, '_secure_read_standing_grant', _REAL_GRANT_READER)
    def write():
        f.registry_path.write_text(json.dumps(f.registry))
        f.registry_path.chmod(0o600)
        owner.STANDING_AUTHORITY_DIRECTORY.mkdir(exist_ok=True)
        path = owner.standing_grant_path(f.scope)
        path.write_text(json.dumps(f.grant))
        path.chmod(0o600)
    f.write = write
    write()
    return f


@pytest.mark.parametrize('damage', ['', 'snapshot_valid', 'delegated_snapshot_valid', 'expired', 'revoked', 'exact_owner', 'profile', 'snapshot_changed', 'provider_unavailable'])
def test_wrapper_real_grant_profile_security_chain(routing_security_fixture, monkeypatch, damage):
    f = routing_security_fixture
    if damage in ('snapshot_valid', 'delegated_snapshot_valid', 'snapshot_changed', 'provider_unavailable'):
        # Snapshot is selected and verified by the real authority code against real profiles.
        monkeypatch.setattr(providers, 'configured_provider_scope', lambda _: asdict(f.scope))
        f.grant['capability_authority'] = providers.select_provider_authority(
            f.runtime, owner_scope=f.scope, profile_key=f.grant['profile_key'])
        if damage in ('delegated_snapshot_valid', 'snapshot_changed'):
            # A new valid delegation target changes the snapshot (profile is available).
            from eimemory.capabilities import CapabilityDefinition
            from eimemory.capabilities.registry import CapabilityRegistry
            CapabilityRegistry(f.runtime.store).register_definition(CapabilityDefinition(
                capability_id='code.implementation', display_name='Provider fixture', description='Fixture',
                owner='fixture', risk_tier='bounded_read', tags=(), provenance={'source': 'test'},
                created_at=datetime.now(timezone.utc).isoformat(), scope='global'), runtime_scope=f.provider_scope)
            CapabilityProfiles(f.runtime.store).register(CapabilityProfile(
                profile_id='isolated.fixture:provider', profile_key=f.grant['profile_key'], revision='provider', scope='global',
                requirements={'code.implementation': {'minimum_maturity': 'evaluated', 'min_pass_rate': 1.0}},
                provenance={'source': 'test'}, created_at=datetime.now(timezone.utc).isoformat()), runtime_scope=f.provider_scope)
            f.entry['profile_digest'] = CapabilityProfiles(f.runtime.store).resolve(
                f.grant['profile_key'], runtime_scope=f.provider_scope, capability_scope='global')['profile']['profile_digest']
            # Keep entry/grant matching so failure must come from live snapshot verification.
            f.grant['profile_digest'] = f.entry['profile_digest']
            # Pin the delegated snapshot before optionally changing the live target.
            monkeypatch.setattr(providers, 'configured_provider_scope', lambda _: asdict(f.provider_scope))
            f.write()
            f.grant['capability_authority'] = providers.select_provider_authority(
                f.runtime, owner_scope=f.scope, profile_key=f.grant['profile_key'])
            if damage == 'snapshot_changed':
                monkeypatch.setattr(providers, 'configured_provider_scope', lambda _: asdict(f.scope))
        elif damage == 'provider_unavailable':
            monkeypatch.setattr(providers, 'configured_provider_scope', lambda _: asdict(f.provider_scope))
    if damage == 'expired':
        f.grant['expires_at'] = '2000-01-01T00:00:00Z'
    elif damage == 'revoked':
        owner.standing_revocation_path(f.scope).touch()
    elif damage == 'exact_owner':
        f.grant['scope']['user_id'] = 'another-owner'
    elif damage == 'profile':
        f.grant['profile_digest'] = f.entry['profile_digest'] = 'f'*64
    f.write()
    expected_errors = dict(expired='standing_grant_expired_or_not_yet_valid', revoked='standing_grant_revoked_or_killed',
        exact_owner='standing_grant_contract_invalid', profile='standing_grant_profile_digest_mismatch',
        snapshot_changed='code_provider_authority_changed', provider_unavailable='profile')
    if damage in expected_errors:
        with pytest.raises((ValueError, RuntimeError), match=expected_errors[damage]):
            grant = owner._grant(f.scope, f.repo, datetime.now(timezone.utc))
            owner._verify_profile(f.runtime, f.scope, grant)
    if damage in ('', 'snapshot_valid', 'delegated_snapshot_valid'):
        grant = owner._grant(f.scope, f.repo, datetime.now(timezone.utc))
        owner._verify_profile(f.runtime, f.scope, grant)
    calls = []
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda runtime, **kw:
        calls.append(asdict(kw['scope'])) or dict(ok=True, execution_ok=True, status='idle'))
    result = maintenance.run_system_code_maintenance(f.runtime, scope=f.provider_scope, owner_id='fixture',
        repo_root=f.repo, include_registered_owners=True)
    allowed = damage in ('', 'snapshot_valid', 'delegated_snapshot_valid')
    if allowed:
        assert providers._secure_delegations() == f.registry
        assert Path(owner._resolve_repo_root(f.repo)) == f.repo
        assert result['ok'], result
    assert calls == ([asdict(f.provider_scope), asdict(f.scope)] if allowed else [])
    assert result['ok'] is allowed


@pytest.mark.parametrize('damage', ['expired', 'revoked'])
def test_real_second_owner_rejection_prevents_all_maintenance(routing_security_fixture, monkeypatch, damage):
    f = routing_security_fixture
    second = ScopeRef(f.scope.tenant_id, f.scope.agent_id, f.scope.workspace_id, 'second')
    grant = deepcopy(f.grant)
    grant['scope'] = asdict(second)
    if damage == 'expired':
        grant['expires_at'] = '2000-01-01T00:00:00Z'
    else:
        owner.standing_revocation_path(second).touch()
    path = owner.standing_grant_path(second)
    path.write_text(json.dumps(grant))
    path.chmod(0o600)
    f.registry['delegations'].append(dict(f.entry, owner_scope=asdict(second)))
    f.write()
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', lambda *args, **kw: pytest.fail('no maintenance before complete admission'))
    result = maintenance.run_system_code_maintenance(f.runtime, scope=f.provider_scope, owner_id='fixture',
        repo_root=f.repo, include_registered_owners=True)
    assert result['ok'] is False and result['primary_report'] is None


def test_real_revocation_after_primary_is_rechecked(routing_security_fixture, monkeypatch):
    f = routing_security_fixture
    primary = dict(ok=True, execution_ok=True, status='processed', checkpoint='real-fixture-result')
    calls = []
    def run(runtime, **kw):
        calls.append(asdict(kw['scope']))
        owner.standing_revocation_path(f.scope).touch()
        return primary
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', run)
    result = maintenance.run_system_code_maintenance(f.runtime, scope=f.provider_scope, owner_id='fixture',
        repo_root=f.repo, include_registered_owners=True)
    assert calls == [asdict(f.provider_scope)]
    assert result['ok'] is False and result['primary_report'] == primary
    assert result['owner_scope_reports'] == []


@pytest.mark.parametrize('primary_ok', [False, True])
def test_real_admission_precedes_primary_and_owner_failure_stops_following(routing_security_fixture, monkeypatch, primary_ok):
    f = routing_security_fixture
    second = ScopeRef(f.scope.tenant_id, f.scope.agent_id, f.scope.workspace_id, 'second')
    grant = deepcopy(f.grant)
    grant['scope'] = asdict(second)
    # The second owner has its own real grant and real exact-owner profile.
    from eimemory.capabilities import CapabilityDefinition
    from eimemory.capabilities.registry import CapabilityRegistry
    CapabilityRegistry(f.runtime.store).register_definition(CapabilityDefinition(
        capability_id='code.implementation', display_name='Second', description='Fixture', owner='fixture',
        risk_tier='bounded_read', tags=(), provenance={'source': 'test'},
        created_at=datetime.now(timezone.utc).isoformat(), scope='global'), runtime_scope=second)
    CapabilityProfiles(f.runtime.store).register(CapabilityProfile(
        profile_id='isolated.fixture:second', profile_key=grant['profile_key'], revision='second', scope='global',
        requirements={'code.implementation': {'minimum_maturity': 'evaluated', 'min_pass_rate': 1.0}},
        provenance={'source': 'test'}, created_at=datetime.now(timezone.utc).isoformat()), runtime_scope=second)
    grant['profile_digest'] = CapabilityProfiles(f.runtime.store).resolve(
        grant['profile_key'], runtime_scope=second, capability_scope='global')['profile']['profile_digest']
    path = owner.standing_grant_path(second)
    path.write_text(json.dumps(grant))
    path.chmod(0o600)
    f.registry['delegations'].append(dict(f.entry, owner_scope=asdict(second), profile_digest=grant['profile_digest']))
    f.write()
    primary = dict(ok=primary_ok, execution_ok=primary_ok, status='processed' if primary_ok else 'blocked', reason='fixture_primary')
    calls = []
    def run(runtime, **kw):
        calls.append(asdict(kw['scope']))
        return primary if len(calls) == 1 else dict(ok=False, status='blocked', reason='fixture_owner')
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance', run)
    result = maintenance.run_system_code_maintenance(f.runtime, scope=f.provider_scope, owner_id='fixture',
        repo_root=f.repo, include_registered_owners=True)
    assert calls == [asdict(f.provider_scope)] + ([asdict(f.scope)] if primary_ok else [])
    assert result['ok'] is False and result['primary_report'] == primary
    assert len(result['owner_scope_reports']) == int(primary_ok)


@pytest.mark.parametrize('failure', ['registry', 'grant'])
@pytest.mark.parametrize('entrypoint', ['watch', 'nightly'])
def test_public_preflight_failure_is_json_fail_closed(monkeypatch, capsys, failure, entrypoint):
    from eimemory.cli import main as cli
    from eimemory.scheduler import jobs

    def rejected(*args):
        raise ValueError('contract_invalid' if failure == 'registry' else 'standing_grant_revoked_or_killed')

    monkeypatch.setattr(providers, '_secure_delegations', rejected if failure == 'registry' else lambda: {
        'delegations': [dict(owner_scope=OWNER, provider_scope=PROVIDER,
                             profile_key='code', profile_digest='a'*64)]})
    monkeypatch.setattr(owner, '_resolve_repo_root', lambda _: Path('/repo'))
    monkeypatch.setattr(owner, '_grant', rejected)
    monkeypatch.setattr(maintenance, '_run_system_code_maintenance',
                        lambda *a, **kw: pytest.fail('preflight must not invoke maintenance effects'))
    if entrypoint == 'watch':
        monkeypatch.setattr('eimemory.governance.world_watchers.collect_world_signals', lambda *a, **kw: {'ok': True})
        monkeypatch.setattr('eimemory.governance.world_watchers.default_watches', lambda: [])
        args = cli._build_parser().parse_args(['learn', 'watch', '--apply'])
        assert cli._cmd_learn(args, runtime=object(), scope=PROVIDER) == 1
        public = json.loads(capsys.readouterr().out)
        assert public['ok'] is False
        report = public['system_code_maintenance']
        for stage in ('runtime_identity', 'system_code_repair', 'system_code_repair_failure', 'code_evolution'):
            assert public[stage] == report[stage]
    else:
        monkeypatch.setenv('EIMEMORY_L5_LOOP_ENABLED', '1')
        monkeypatch.setenv('EIMEMORY_L5_LOOP_APPLY', '1')
        report = json.loads(json.dumps(jobs._run_code_evolution_maintenance(object(), scope=PROVIDER)))
    assert report['schema'] == 'system_code_maintenance.v1'
    assert report['report_type'] == 'code_evolution_maintenance'
    assert report['ok'] is False and report['execution_ok'] is False
    assert report['status'] == 'blocked' and report['repair_complete'] is False
    assert report['primary_report'] is None and report['owner_scope_reports'] == []
    assert report['owner_routing_reason'] == 'owner_scope_authority_error:ValueError'
    for stage in ('runtime_identity', 'native_policy_owner', 'system_code_repair', 'system_code_repair_failure', 'code_evolution'):
        assert report[stage] == {'ok': False, 'status': 'not_run', 'reason': 'owner_scope_authority_error:ValueError'}


@pytest.mark.parametrize('recovery_ok', [False, True])
def test_recovery_only_return_retains_stage_contract_without_native_effects(monkeypatch, recovery_ok):
    from types import SimpleNamespace
    calls = []
    recovery = dict(ok=recovery_ok, status='idle' if recovery_ok else 'blocked')
    runtime = SimpleNamespace(resume_code_evolution_transactions=lambda **kw: calls.append(kw) or recovery)
    monkeypatch.setattr(providers, '_secure_delegations', lambda: pytest.fail('recovery-only discovery'))
    report = json.loads(json.dumps(maintenance.run_system_code_maintenance(
        runtime, scope=PROVIDER, owner_id='nightly', detect_and_route=False, include_registered_owners=True)))
    assert len(calls) == 1
    assert report['ok'] is recovery_ok and report['execution_ok'] is recovery_ok
    assert report['code_evolution'] == recovery
    assert report['schema'] == 'system_code_maintenance.v1'
    assert report['repair_complete'] is False
    for stage in ('runtime_identity', 'native_policy_owner', 'system_code_repair', 'system_code_repair_failure'):
        assert report[stage] == {'ok': False, 'status': 'not_run', 'reason': 'system_detection_disabled'}


def test_dry_run_contract_is_json_without_native_effects(monkeypatch):
    monkeypatch.setattr(providers, '_secure_delegations', lambda: pytest.fail('dry-run discovery'))
    report = json.loads(json.dumps(maintenance.run_system_code_maintenance(
        object(), scope=PROVIDER, owner_id='watch', apply=False, include_registered_owners=True)))
    assert report['schema'] == 'system_code_maintenance.v1'
    assert report['status'] == 'skipped' and report['repair_complete'] is False
    for stage in ('runtime_identity', 'native_policy_owner', 'system_code_repair', 'system_code_repair_failure', 'code_evolution'):
        assert report[stage] == {'ok': True, 'status': 'skipped', 'reason': 'apply_disabled'}
