"""Isolated fixtures only: no production grant, incident, provenance or elapsed time.

Git/issuer/capture/source/record and transaction SQLite paths are native. The
provider readiness boundary and machine root permissions are explicit fixture
substitutes on Windows; these tests do not claim production provider readiness.
Imported by the existing CI-selected system_code_maintenance module.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.contracts.release_identity import ReleaseIdentity
from eimemory.governance.evolution import code_automation_policy_owner as owner
from eimemory.governance.evolution import code_automation_policy_capture as capture_owner
from eimemory.governance.evolution import code_automation_policy as policy_api
from eimemory.governance.evolution.code_automation_policy_issue import _EFFECTS_BY_MODE
from eimemory.governance.evolution.code_evolution_repository import remote_url_digest
from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
from eimemory.models.records import ScopeRef
from eimemory.ops.closure_capture import process_capture
from eimemory.storage.code_evolution_store import CodeEvolutionStore
from eimemory.storage.atomic_file import atomic_write_json
from eimemory.adapters.hermes.code_implementation import resolve_code_implementation_provider as _REAL_PROVIDER_RESOLVER
from eimemory.capabilities.profiles import CapabilityProfiles
_REAL_PROFILE_RESOLVER = CapabilityProfiles.resolve
from test_closure_pipeline_contract import blocked
from release_report_fixtures import complete_report


@pytest.fixture
def native_policy_fixture(tmp_path, monkeypatch):
    repo = tmp_path / 'fixture-git'
    subprocess.run(['git', 'clone', '--quiet', '--shared', str(Path(__file__).resolve().parents[1]), str(repo)], check=True)
    commit = owner._git(repo, 'rev-parse', 'HEAD')
    branch = owner._git(repo, 'symbolic-ref', '--short', 'HEAD')
    scope = ScopeRef(tenant_id='fixture-tenant', agent_id='fixture-agent', workspace_id='fixture-workspace', user_id='fixture-user')
    runtime = Runtime.create(root=tmp_path/'fixture-runtime')
    release = ReleaseIdentity(commit, 'fixture-version', 'fixture-receipt', 'fixture-session')
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity', lambda *_a, **_k: release)
    monkeypatch.setattr('eimemory.config.trusted.trusted_repository_root', lambda: repo)
    monkeypatch.setattr('eimemory.config.trusted.trusted_remote', lambda: 'origin')
    monkeypatch.setattr('eimemory.config.trusted.trusted_branch_allowed', lambda b: b == branch)
    destination = tmp_path/'fixture-etc'/'policy.json'
    monkeypatch.setattr(policy_api, 'CODE_AUTOMATION_POLICY_DEFAULT_PATH', destination)
    monkeypatch.setattr(policy_api, 'CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH', tmp_path/'fixture-kill')
    monkeypatch.delenv(policy_api.CODE_EVOLUTION_KILL_SWITCH_ENV, raising=False)
    monkeypatch.setattr(owner, 'STANDING_AUTHORITY_DIRECTORY', tmp_path/'fixture-root-authority')
    now = datetime.now(timezone.utc)
    kind = 'release.closure_internal_failure'
    source, plan = owner._ROUTES[kind]
    grant = {'schema': 'code_automation_standing_grant.v1', 'grant_id': 'isolated-fixture-only',
             'enabled': True, 'not_before': (now-timedelta(minutes=1)).isoformat(),
             'expires_at': (now+timedelta(hours=6)).isoformat(), 'scope': asdict(scope),
             'repository': {'root': str(repo), 'remote': 'origin', 'branch': branch,
                            'remote_url_digest': remote_url_digest(owner._git(repo, 'remote', 'get-url', 'origin'))},
             'profile_key': 'isolated.fixture', 'profile_digest': sha256(b'fixture-profile').hexdigest(),
             'routes': {kind: {'source': source, 'detector_id': source+'.v1',
             'test_plan_id': plan, 'test_plan_digest': protected_test_plan_digest(plan)}},
             'effects': dict(_EFFECTS_BY_MODE['full']), 'max_transactions': 1}
    monkeypatch.setattr(owner, '_secure_read_standing_grant', lambda *_a: deepcopy(grant))
    monkeypatch.setattr('eimemory.capabilities.profiles.CapabilityProfiles.resolve',
                        lambda *_a, **_k: {'profile': {'profile_digest': grant['profile_digest'],
                            'profile_id': 'isolated.fixture:r1', 'profile_key': grant['profile_key']},
                            'requirements': [{'capability_id': 'code.implementation'}]})
    from eimemory.adapters.hermes.code_implementation import IMPLEMENTATION_DIGEST
    provider = {'ok': True, 'provider_ready': True, 'advertisement_fresh': True, 'health': {'ok': True},
                'implementation_digest': IMPLEMENTATION_DIGEST, 'advertisement_id': 'fixture-ad',
                'advertisement_digest': sha256(b'fixture-ad').hexdigest(), 'catalog_case_id': 'fixture-catalog',
                'catalog_snapshot_digest': sha256(b'fixture-catalog').hexdigest(),
                'catalog_activation_state_digest': sha256(b'fixture-activation').hexdigest()}
    monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',
                        lambda *_a, **_k: deepcopy(provider))
    if os.name != 'posix':
        # Native Linux tests exercise the real uid/mode/no-follow policy loader.
        def fixture_secure_read(path):
            error = policy_api._v2_no_symlink(Path(path), missing_ok=False)
            return ('', error) if error else (Path(path).read_text(encoding='utf8'), '')
        monkeypatch.setattr(policy_api, '_secure_read_v2_policy', fixture_secure_read)
    def capture(report=None, *, inspect=False, attempt='fixture-attempt'):
        value = deepcopy(report) if report is not None else blocked('storage_failed')
        if report is not None:
            def bind(value, key=''):
                if isinstance(value, dict): return {k: bind(v, k) for k, v in value.items()}
                if isinstance(value, list): return [bind(v) for v in value]
                if isinstance(value, str):
                    if key == 'release_session_id': return release.session_id
                    return value.replace('a'*40, commit).replace('receipt-1', release.receipt_id)
                return value
            value = bind(value)
        value['deployment'] = {**value.get('deployment', {}), 'commit': commit,
                               'promotion_request_id': release.receipt_id}
        value['deployment_receipt'] = {**value.get('deployment_receipt', {}), 'release_session_id': release.session_id}
        source_path = tmp_path/'fixture-report.json'
        source_path.write_text(json.dumps(value), encoding='utf8')
        return process_capture(source_path, evidence_dir=runtime.store.root/'logs'/'release-closure-captures',
             scope=asdict(scope), expected_commit=commit, attempt_id=attempt, inspect_only=inspect,
             runtime_factory=lambda: SimpleNamespace(store=runtime.store, close=lambda: None))
    fixture = SimpleNamespace(repo=repo, runtime=runtime, scope=scope, release=release, grant=grant,
                              destination=destination, provider=provider, capture=capture, root=tmp_path)
    try:
        yield fixture
    finally:
        runtime.close()


def _ensure(f, **kwargs):
    return owner.ensure_native_incident_policy(f.runtime, scope=f.scope, repo_root=f.repo, **kwargs)


def _state(f):
    return f.runtime.store.root/'state'/'code-automation-owner'


def test_native_policy_no_incident_and_dry_run_write_no_authority(native_policy_fixture, monkeypatch):
    f = native_policy_fixture
    monkeypatch.setattr(owner, '_secure_read_standing_grant', lambda *_a: pytest.fail('idle must not read grant'))
    assert _ensure(f)['status'] == 'idle'
    assert _ensure(f, apply=False)['status'] == 'skipped'
    assert not f.destination.exists() and not _state(f).exists()
    f.capture()
    assert _ensure(f, apply=False)['status'] == 'skipped'
    assert not f.destination.exists() and not _state(f).exists()


def test_native_policy_real_capture_git_issuer_sqlite_and_same_original_bytes(native_policy_fixture):
    f = native_policy_fixture
    captured = f.capture()
    assert captured['status'] == 'failure_detected', captured
    result = _ensure(f)
    assert result['ok'] is True and result['status'] == 'available', result
    original = f.destination.read_bytes()
    policy = json.loads(original)
    assert policy['incident']['incident_digest'] == f.runtime.store.get_by_id(captured['incident_record_id']).meta['incident_digest']
    assert policy['repository']['base_commit'] == f.release.commit
    assert policy['max_transactions'] == 1 and policy['deployment']['observation_seconds'] == 28800
    assert all(policy['effects'].values())
    assert result['repair_complete'] is False
    receipt = f.runtime.store.get_by_id(result['delegation_record_id'])
    assert receipt.content['terminal_evidence'] is False and receipt.provenance['origin'] == 'native_policy_owner'
    assert 'known_before_detection' not in receipt.provenance
    replay = _ensure(f)
    assert replay['delegation_record_id'] == result['delegation_record_id']
    assert f.destination.read_bytes() == original
    assert len(list(_state(f).glob('*.json'))) == 2


@pytest.mark.parametrize('bad', ['missing', 'disabled', 'scope', 'remote', 'branch', 'repo', 'effects',
                                 'max_transactions', 'expiry', 'route', 'test_plan', 'profile', 'kill', 'revoke'])
def test_native_policy_grant_boundary_is_default_deny(native_policy_fixture, monkeypatch, bad):
    f = native_policy_fixture
    f.capture()
    if bad == 'missing':
        monkeypatch.setattr(owner, '_secure_read_standing_grant', lambda *_a: (_ for _ in ()).throw(ValueError('standing_grant_missing')))
    elif bad == 'disabled': f.grant['enabled'] = False
    elif bad == 'scope': f.grant['scope']['user_id'] = 'other'
    elif bad in {'remote', 'branch'}: f.grant['repository'][bad] = 'other'
    elif bad == 'repo': f.grant['repository']['root'] = str(f.root)
    elif bad == 'effects': f.grant['effects']['push'] = False
    elif bad == 'max_transactions': f.grant['max_transactions'] = True
    elif bad == 'expiry': f.grant['expires_at'] = '2020-01-01T00:00:00Z'
    elif bad == 'route': f.grant['routes']['release.closure_internal_failure']['source'] = 'user'
    elif bad == 'test_plan': f.grant['routes']['release.closure_internal_failure']['test_plan_digest'] = 'f'*64
    elif bad == 'profile': f.grant['profile_key'] = ''
    elif bad == 'kill': policy_api.CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH.write_text('stop')
    elif bad == 'revoke':
        owner.STANDING_AUTHORITY_DIRECTORY.mkdir()
        (owner.STANDING_AUTHORITY_DIRECTORY/'standing-grant.revoked').write_text('revoked')
    assert _ensure(f)['ok'] is False
    assert not _state(f).exists() and not f.destination.exists()


@pytest.mark.parametrize('bad', ['provider', 'advertisement', 'health', 'implementation'])
def test_native_policy_live_provider_boundary(native_policy_fixture, bad):
    f = native_policy_fixture
    f.capture()
    if bad == 'provider': f.provider['ok'] = False
    elif bad == 'advertisement': f.provider['advertisement_fresh'] = False
    elif bad == 'health': f.provider['health']['ok'] = False
    else: f.provider['implementation_digest'] = 'f'*64
    assert _ensure(f)['ok'] is False
    assert not f.destination.exists()


def test_native_policy_manual_policy_is_preserved_without_root_grant(native_policy_fixture, monkeypatch):
    f = native_policy_fixture
    f.capture()
    f.destination.parent.mkdir()
    f.destination.write_bytes(b'{"manual":"existing"}\n')
    os.chmod(f.destination, 0o600)
    monkeypatch.setattr(owner, '_secure_read_standing_grant', lambda *_a: pytest.fail('manual policy must be preserved'))
    assert _ensure(f)['status'] == 'manual_policy_preserved'
    assert f.destination.read_bytes() == b'{"manual":"existing"}\n'
    assert not _state(f).exists()


@pytest.mark.parametrize('phase', ['before_publish', 'after_publish', 'after_receipt'])
def test_native_policy_crash_recovery_reuses_original_intent_and_clock(native_policy_fixture, monkeypatch, phase):
    f = native_policy_fixture
    f.capture()
    install = owner._install_policy_atomically
    append = f.runtime.store.append
    if phase == 'before_publish':
        monkeypatch.setattr(owner, '_install_policy_atomically', lambda *_a, **_k: 'fixture-crash-before-publish')
    elif phase == 'after_publish':
        def fail_receipt(*args, **kwargs):
            if args[0].source == owner.SOURCE: raise OSError('fixture-crash-after-publish')
            return append(*args, **kwargs)
        monkeypatch.setattr(f.runtime.store, 'append', fail_receipt)
    else:
        atomic = owner.atomic_write_json
        def fail_pointer(path, value):
            if Path(path).name == 'published.json': raise OSError('fixture-crash-after-receipt')
            return atomic(path, value)
        monkeypatch.setattr(owner, 'atomic_write_json', fail_pointer)
    first = _ensure(f)
    assert first['ok'] is False
    intent_path = next(_state(f).glob('*.json'))
    intent_raw = intent_path.read_bytes()
    original = json.loads(intent_raw)['policy_bytes'].encode()
    monkeypatch.setattr(owner, '_install_policy_atomically', install)
    monkeypatch.setattr(f.runtime.store, 'append', append)
    if phase == 'after_receipt': monkeypatch.setattr(owner, 'atomic_write_json', atomic)
    replay = _ensure(f)
    assert replay['ok'] is True, replay
    assert intent_path.read_bytes() == intent_raw and f.destination.read_bytes() == original


def test_native_policy_parallel_manual_publication_is_never_overwritten(native_policy_fixture, monkeypatch):
    f = native_policy_fixture
    f.capture()
    install = owner._install_policy_atomically
    manual = {'fixture': 'concurrent-manual'}
    def interleaved(destination, policy, **kwargs):
        assert install(destination, manual) == ''
        return install(destination, policy, **kwargs)
    monkeypatch.setattr(owner, '_install_policy_atomically', interleaved)
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'existing_policy_changed'
    assert json.loads(f.destination.read_bytes()) == manual


def test_native_policy_latest_same_release_success_supersedes_old_failure(native_policy_fixture):
    f = native_policy_fixture
    failed = f.capture()
    passed = f.capture(complete_report(), attempt='fixture-new-success')
    assert passed['recording_ok'] is True and not passed['incident_record_id'], passed
    assert f.runtime.store.get_by_id(failed['incident_record_id']).status == 'active'
    result = _ensure(f)
    assert result['ok'] is True and result['status'] == 'idle', result
    assert not _state(f).exists() and not f.destination.exists()


@pytest.mark.parametrize('bad', ['inflight', 'partial', 'source', 'decision', 'receipt'])
def test_native_policy_latest_invalid_capture_never_falls_back_to_old(native_policy_fixture, bad):
    f = native_policy_fixture
    first = f.capture()
    directory = Path(first['capture']['path'])
    if bad == 'inflight':
        newer = directory.parent/'closure-fixture-inflight'
        newer.mkdir()
        capture_owner.begin_current_capture(newer, scope=asdict(f.scope), commit=f.release.commit)
    elif bad == 'partial': (directory/'result.json').unlink()
    elif bad == 'source': (directory/'source.bin').write_text('{}')
    elif bad == 'decision':
        value = json.loads((directory/'summary.json').read_bytes()); value['disposition'] = 'accepted'
        atomic_write_json(directory/'summary.json', value)
    else:
        f.release = ReleaseIdentity(f.release.commit, f.release.version, 'another-receipt', f.release.session_id)
        # fixture current_release closure holds the original identity; alter the physical record's receipt instead.
        value = json.loads((directory/'source.bin').read_bytes()); value['deployment']['promotion_request_id'] = 'another-receipt'
        (directory/'source.bin').write_text(json.dumps(value))
    assert _ensure(f)['ok'] is False
    assert not f.destination.exists() and not _state(f).exists()


def test_native_policy_older_finisher_cannot_replace_new_capture(native_policy_fixture):
    f = native_policy_fixture
    first = f.capture()
    second = f.capture(attempt='fixture-newer')
    capture_owner.finish_current_capture(Path(first['capture']['path']), first)
    marker = capture_owner._private_json(capture_owner.capture_marker(Path(second['capture']['path']).parent, asdict(f.scope), f.release.commit))
    assert marker['capture_id'] == second['capture']['capture_id']
    result = _ensure(f)
    assert result['ok'] is True, result
    assert result['incident_record_id'] == second['incident_record_id']


def test_native_policy_renewing_grant_does_not_reset_existing_one_shot(native_policy_fixture):
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    original = f.destination.read_bytes()
    f.grant['grant_id'] = 'fixture-renewed-standing-grant'
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'existing_intent_authority_changed'
    assert f.destination.read_bytes() == original


def test_native_policy_authority_paths_are_deny_self():
    from eimemory.governance.evolution.code_evolution_path_policy import path_allowed_for_evolution
    # The public path helper includes the invariant deny-self authority radius.
    for path in ['eimemory/governance/evolution/code_automation_policy_owner.py',
                 'eimemory/governance/evolution/code_automation_policy_capture.py',
                 'eimemory/governance/evolution/code_automation_policy_incidents.py',
                 'eimemory/ops/closure_capture.py']:
        assert path_allowed_for_evolution(path, allowed_path_globs=('eimemory/**',)) == (False, 'deny_self')


def test_native_policy_consumed_ledger_survives_policy_removal_and_replay(native_policy_fixture):
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    ledger = CodeEvolutionStore(f.runtime.store)
    ledger.create_transaction({'transaction_id': 'fixture-consumed-tx', 'idempotency_key': 'fixture-consumed-tx',
        'incident': {'incident_id': first['incident_record_id']},
        'scope': asdict(f.scope), 'repository': {'root': str(f.repo), 'ref': 'master'},
        'current_state': 'DETECTED', 'terminal': False})
    material = {'transaction_id': 'fixture-consumed-tx', 'policy_digest': first['policy_digest']}
    from eimemory.storage.code_evolution_store import digest_json
    ledger.consume_policy(transaction_id='fixture-consumed-tx', policy_digest=first['policy_digest'],
        authorization_receipt_digest=digest_json(material), payload={'authorization_material': material,
            'authorized_policy': {'ok': True, 'policy_digest': first['policy_digest']}})
    original = deepcopy(ledger.get_policy_consumption(first['policy_digest']))
    f.destination.unlink()
    result = _ensure(f)
    assert result['status'] == 'consumed' and result['transaction_id'] == 'fixture-consumed-tx', result
    assert not f.destination.exists()
    assert ledger.get_policy_consumption(first['policy_digest']) == original


@pytest.mark.parametrize('terminal', [True, False])
def test_native_policy_new_incident_handoff_respects_actual_sqlite_terminal(native_policy_fixture, terminal):
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    ledger = CodeEvolutionStore(f.runtime.store)
    tx = ledger.create_transaction({'transaction_id': 'fixture-prior-tx', 'idempotency_key': 'fixture-prior-tx',
        'incident': {'incident_id': first['incident_record_id']}, 'scope': asdict(f.scope),
        'repository': {'root': str(f.repo), 'ref': 'master'}, 'current_state': 'DETECTED', 'terminal': False})
    material = {'transaction_id': tx['transaction_id'], 'policy_digest': first['policy_digest']}
    from eimemory.storage.code_evolution_store import digest_json
    ledger.consume_policy(transaction_id=tx['transaction_id'], policy_digest=first['policy_digest'],
        authorization_receipt_digest=digest_json(material), payload={'authorization_material': material,
            'authorized_policy': {'ok': True, 'policy_digest': first['policy_digest']}})
    consumed = deepcopy(ledger.get_policy_consumption(first['policy_digest']))
    if terminal:
        # A real isolated abort receipt proves terminal handoff, without
        # inventing deployment, successful effects, novelty or 8h observation.
        from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
        CodeEvolutionTransactionManager(f.runtime).effect_disabled(tx['transaction_id'], step='fixture-no-effects-settled')
        assert type(ledger.get_transaction(tx['transaction_id'])['terminal']) is int
    captured = f.capture(attempt='fixture-next-real-capture')
    result = _ensure(f)
    if terminal:
        assert result['ok'] is True and result['status'] == 'available', result
        assert result['policy_digest'] != first['policy_digest']
        assert result['incident_record_id'] == captured['incident_record_id']
    else:
        assert result['ok'] is False and result['reason'] == 'repository_transaction_active'
    assert ledger.get_policy_consumption(first['policy_digest']) == consumed


@pytest.mark.parametrize('state', ['ABORTED_NO_EXTERNAL_EFFECT', 'DETECTED'])
def test_native_policy_terminal_flag_without_native_receipt_cannot_fund_handoff(native_policy_fixture, state):
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    ledger = CodeEvolutionStore(f.runtime.store)
    from eimemory.storage.code_evolution_store import digest_json
    material = {'transaction_id': 'fixture-receiptless', 'policy_digest': first['policy_digest']}
    authorized = {'ok': True, 'policy_digest': first['policy_digest']}
    digest = digest_json(material)
    # Deliberately malformed isolated ledger input exercises the real legacy
    # INSERT gap; this is explicitly not a terminal proof or production sample.
    tx = ledger.create_transaction({'transaction_id': 'fixture-receiptless', 'idempotency_key': 'fixture-receiptless',
        'incident': {'incident_id': first['incident_record_id']}, 'scope': asdict(f.scope),
        'repository': {'root': str(f.repo), 'ref': 'master'}, 'current_state': state, 'terminal': True,
        'policy_digest': first['policy_digest'], 'authorization_digest': digest, 'authorized_policy': authorized})
    ledger.consume_policy(transaction_id=tx['transaction_id'], policy_digest=first['policy_digest'],
        authorization_receipt_digest=digest, payload={'authorization_material': material, 'authorized_policy': authorized})
    assert ledger.get_terminal_receipt(tx['transaction_id']) is None
    f.capture(attempt='fixture-next-current')
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'prior_policy_terminal_receipt_unverified', result


def test_native_policy_unresolved_real_quarantine_still_blocks_next_grant(native_policy_fixture):
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    manager = CodeEvolutionTransactionManager(f.runtime)
    tx = manager.create_detected({'transaction_id': 'fixture-quarantined', 'idempotency_key': 'fixture-quarantined',
        'incident': {'incident_id': first['incident_record_id']}, 'scope': asdict(f.scope),
        'repository': {'root': str(f.repo), 'ref': 'master'}})
    manager.terminalize(tx['transaction_id'], {'outcome': 'recovery_quarantined', 'evidence_digest': 'f'*64},
                         terminal_state='RECOVERY_QUARANTINED')
    f.capture(attempt='fixture-next-current')
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'repository_transaction_active', result


def test_native_policy_replay_requires_parent_durability_after_replace_error(native_policy_fixture, monkeypatch):
    import eimemory.storage.atomic_file as atomic
    f = native_policy_fixture
    f.capture()
    fsync = atomic._fsync_directory
    calls = []
    def fixture_failure(path):
        if Path(path) == f.destination.parent:
            calls.append(path)
            raise OSError('fixture-directory-durability-unavailable')
        return fsync(path)
    monkeypatch.setattr(atomic, '_fsync_directory', fixture_failure)
    first = _ensure(f)
    assert first['ok'] is False and f.destination.exists()
    original = f.destination.read_bytes()
    intent = next(_state(f).glob('*.json')).read_bytes()
    second = _ensure(f)
    assert second['ok'] is False and len(calls) == 2
    assert f.destination.read_bytes() == original and next(_state(f).glob('*.json')).read_bytes() == intent
    monkeypatch.setattr(atomic, '_fsync_directory', fsync)
    third = _ensure(f)
    assert third['ok'] is True and third['status'] == 'available', third
    assert f.destination.read_bytes() == original


def test_native_standing_grant_preparation_uses_real_coordinates_without_an_incident(native_policy_fixture):
    from eimemory.governance.evolution.code_automation_policy_grant import prepare_standing_grant
    f = native_policy_fixture
    prepared = prepare_standing_grant(f.runtime, scope=f.scope, profile_key=f.grant['profile_key'], valid_hours=2)
    grant = prepared['grant']
    assert grant['scope'] == asdict(f.scope) and grant['repository'] == f.grant['repository']
    assert grant['profile_digest'] == f.grant['profile_digest']
    assert set(grant['routes']) == {'deployment.runtime_commit_drift', 'release.closure_internal_failure', 'release.closure_report_failure'}
    assert prepared['repository_commit'] == f.release.commit and prepared['receipt_id'] == f.release.receipt_id
    assert prepared['child_policy_issued'] is False and prepared['incident_created'] is False and prepared['l5_origin_verified'] is False
    assert not f.destination.exists() and not owner.standing_grant_path(f.scope).exists() and not _state(f).exists()


def test_native_standing_grant_indefinite_authority_has_finite_original_child(native_policy_fixture):
    from eimemory.governance.evolution.code_automation_policy_grant import prepare_standing_grant
    f = native_policy_fixture
    prepared = prepare_standing_grant(f.runtime, scope=f.scope, profile_key=f.grant['profile_key'])
    assert prepared['grant']['expires_at'] is None
    f.grant['expires_at'] = None
    f.capture()
    result = _ensure(f)
    assert result['ok'] is True, result
    policy = json.loads(f.destination.read_bytes())
    assert datetime.fromisoformat(policy['expires_at']) - datetime.fromisoformat(policy['not_before']) == timedelta(days=2)
    assert policy['max_transactions'] == 1 and policy['deployment']['observation_seconds'] == 28800


@pytest.mark.parametrize('bad', ['', False, 0, '2026-10-19T00:00:00', 'missing'])
def test_native_standing_grant_expiry_requires_explicit_null_or_aware_time(native_policy_fixture, bad):
    f = native_policy_fixture
    if bad == 'missing': f.grant.pop('expires_at')
    else: f.grant['expires_at'] = bad
    with pytest.raises((TypeError, ValueError)):
        owner._validate_grant(f.grant, f.scope, f.repo, datetime.now(timezone.utc))
    assert not _state(f).exists() and not f.destination.exists()


def test_native_standing_grant_scope_paths_and_revocation_are_exact(native_policy_fixture):
    f = native_policy_fixture
    exact = asdict(f.scope)
    paths = {owner.standing_grant_path(exact)}
    for field in exact:
        other = {**exact, field: exact[field] + '-other'}
        paths.add(owner.standing_grant_path(other))
    assert len(paths) == 5
    assert owner.standing_grant_path(dict(reversed(list(exact.items())))) == owner.standing_grant_path(exact)
    owner.STANDING_AUTHORITY_DIRECTORY.mkdir()
    other = ScopeRef.from_dict({**exact, 'user_id': 'another-real-owner'})
    owner.standing_revocation_path(other).write_text('scope revoked')
    owner._validate_grant(f.grant, f.scope, f.repo, datetime.now(timezone.utc))
    owner.standing_revocation_path(f.scope).write_text('scope revoked')
    with pytest.raises(ValueError, match='revoked'):
        owner._validate_grant(f.grant, f.scope, f.repo, datetime.now(timezone.utc))
    owner.standing_revocation_path(f.scope).unlink()
    (owner.STANDING_AUTHORITY_DIRECTORY/'standing-grant.revoked').write_text('global revoked')
    with pytest.raises(ValueError, match='revoked'):
        owner._validate_grant(f.grant, f.scope, f.repo, datetime.now(timezone.utc))


def test_native_standing_grant_preparation_rejects_empty_profile(native_policy_fixture, monkeypatch):
    from eimemory.governance.evolution.code_automation_policy_grant import prepare_standing_grant
    f = native_policy_fixture
    monkeypatch.setattr(CapabilityProfiles, 'resolve', lambda *_a, **_k: {'profile': {}, 'requirements': []})
    with pytest.raises(ValueError, match='code_implementation_profile_required'):
        prepare_standing_grant(f.runtime, scope=f.scope, profile_key=f.grant['profile_key'])
    assert not owner.standing_grant_path(f.scope).exists()


def test_native_standing_grant_start_requires_timezone(native_policy_fixture):
    f = native_policy_fixture
    f.grant['not_before'] = '2020-01-01T00:00:00'
    with pytest.raises(ValueError, match='time_invalid'):
        owner._validate_grant(f.grant, f.scope, f.repo, datetime.now(timezone.utc))


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='isolated real root installation filesystem test')
def test_native_standing_grant_root_install_is_digest_reviewed_and_issues_no_child(native_policy_fixture, monkeypatch):
    import pwd
    from eimemory.governance.evolution.code_automation_policy_grant import prepare_standing_grant, install_prepared_standing_grant
    f = native_policy_fixture
    account = pwd.getpwnam('nobody')
    f.scope.user_id = 'ou_fixture_logical_channel_owner'
    prepared = prepare_standing_grant(f.runtime, scope=f.scope, profile_key=f.grant['profile_key'], valid_hours=2)
    monkeypatch.setattr(owner, '_secure_read_standing_grant', _REAL_STANDING_READER)
    with pytest.raises(ValueError, match='review_digest'):
        install_prepared_standing_grant(prepared, expected_grant_digest='f'*64, runtime_user=account.pw_name)
    assert not owner.standing_grant_path(f.scope).exists()
    result = install_prepared_standing_grant(prepared, expected_grant_digest=prepared['grant_digest'], runtime_user=account.pw_name)
    info = owner.standing_grant_path(f.scope).stat()
    assert result['status'] == 'installed' and info.st_uid == 0 and info.st_gid == account.pw_gid
    assert info.st_mode & 0o777 == 0o640 and owner.standing_grant_path(f.scope).parent.stat().st_mode & 0o777 == 0o750
    assert not f.destination.exists() and not _state(f).exists()
    same = install_prepared_standing_grant(prepared, expected_grant_digest=prepared['grant_digest'], runtime_user=account.pw_name)
    assert same['status'] == 'existing'
    other = deepcopy(prepared); other['grant']['grant_id'] = 'fixture-replacement'
    from eimemory.governance.release.closure_verdict import report_digest
    other['grant_digest'] = report_digest(other['grant'])
    with pytest.raises(ValueError, match='existing_authority_preserved'):
        install_prepared_standing_grant(other, expected_grant_digest=other['grant_digest'], runtime_user=account.pw_name)


def test_native_policy_parallel_owners_create_one_original_intent_and_receipt(native_policy_fixture):
    from concurrent.futures import ThreadPoolExecutor
    f = native_policy_fixture
    f.capture()
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: _ensure(f), range(2)))
    assert all(r['ok'] is True for r in reports), reports
    assert reports[0]['policy_digest'] == reports[1]['policy_digest']
    assert reports[0]['delegation_record_id'] == reports[1]['delegation_record_id']
    intents = [p for p in _state(f).glob('*.json') if p.name != 'published.json']
    assert len(intents) == 1
    assert json.loads(intents[0].read_bytes())['policy_bytes'].encode() == f.destination.read_bytes()


@pytest.mark.parametrize('change', ['kill', 'new_capture', 'grant', 'git'])
def test_native_policy_late_authority_change_stops_publication(native_policy_fixture, monkeypatch, change):
    f = native_policy_fixture
    f.capture()
    atomic = owner.atomic_write_json
    def interleave(path, value):
        atomic(path, value)
        if Path(path).name == 'published.json': return
        if change == 'kill': policy_api.CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH.write_text('stop')
        elif change == 'new_capture':
            path = f.runtime.store.root/'logs'/'release-closure-captures'/'closure-fixture-new-inflight'
            path.mkdir()
            capture_owner.begin_current_capture(path, scope=asdict(f.scope), commit=f.release.commit)
        elif change == 'grant': f.grant['grant_id'] = 'fixture-grant-changed'
        elif change == 'git': (f.repo/'README.md').write_text('fixture-dirty')
    monkeypatch.setattr(owner, 'atomic_write_json', interleave)
    result = _ensure(f)
    assert result['ok'] is False, result
    assert not f.destination.exists()


def test_native_policy_changed_original_bytes_are_not_silently_republished(native_policy_fixture):
    f = native_policy_fixture
    f.capture()
    first = _ensure(f)
    assert first['ok'] is True, first
    changed = json.dumps(json.loads(f.destination.read_bytes()), sort_keys=True).encode()
    f.destination.write_bytes(changed)
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'published_policy_bytes_changed'
    assert f.destination.read_bytes() == changed


def test_native_policy_inspection_does_not_supersede_native_capture(native_policy_fixture):
    f = native_policy_fixture
    first = f.capture()
    marker_path = capture_owner.capture_marker(Path(first['capture']['path']).parent, asdict(f.scope), f.release.commit)
    original = marker_path.read_bytes()
    f.capture(complete_report(), inspect=True)
    assert marker_path.read_bytes() == original
    assert _ensure(f)['ok'] is True


def test_native_policy_rejects_forged_negative_source_facts_even_with_fresh_hash(native_policy_fixture, monkeypatch):
    from eimemory.contracts.code_incident_provenance import provenance_snapshot
    f = native_policy_fixture
    capture = f.capture()
    original = f.runtime.store.get_by_id(capture['incident_record_id'])
    forged = deepcopy(original)
    report = forged.content['detector_report']
    facts = {**report['source_facts']['facts'], 'known_before_detection': False,
             'prior_user_reported': False, 'manual_bootstrap': False}
    report.update(facts)
    report['source_facts'] = provenance_snapshot(facts)
    forged.provenance = facts
    monkeypatch.setattr(f.runtime.store, 'list_records_by_sources_exact_scope', lambda **_k: [forged])
    result = _ensure(f)
    assert result['ok'] is False and result['reason'] == 'current_incident_source_unverified', result
    assert not f.destination.exists() and not _state(f).exists()


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='real root-owned standing grant fixture requires native root uid')
@pytest.mark.parametrize('bad', ['valid', 'parent_writable', 'file_writable', 'link', 'hardlink', 'nonroot_owner', 'revoked', 'other_scope_only'])
def test_native_root_standing_grant_filesystem_security(native_policy_fixture, monkeypatch, bad):
    f = native_policy_fixture
    reader = __import__(owner.__name__, fromlist=['_secure_read_standing_grant'])
    # The fixture deliberately substitutes this reader; recover the real function
    # through its saved function below, never replace os.geteuid to claim ownership.
    path = owner.standing_grant_path(f.scope)
    path.parent.mkdir(mode=0o750)
    path.write_text(json.dumps(f.grant))
    path.chmod(0o640)
    if bad == 'parent_writable': path.parent.chmod(0o770)
    elif bad == 'file_writable': path.chmod(0o660)
    elif bad == 'link':
        other = path.with_name('original.json'); path.rename(other); path.symlink_to(other)
    elif bad == 'hardlink': os.link(path, path.with_name('other.json'))
    elif bad == 'nonroot_owner': os.chown(path, 65534, 65534)
    elif bad == 'revoked': path.with_name('standing-grant.revoked').write_text('revoked')
    elif bad == 'other_scope_only':
        other = ScopeRef.from_dict({**asdict(f.scope), 'user_id': 'other-owner'})
        path.rename(owner.standing_grant_path(other))
    monkeypatch.setattr(owner, '_secure_read_standing_grant', _REAL_STANDING_READER)
    f.capture()
    result = _ensure(f)
    assert result['ok'] is (bad == 'valid'), result
    if bad != 'valid': assert not f.destination.exists()


_REAL_STANDING_READER = owner._secure_read_standing_grant


@pytest.mark.skipif(os.name != 'posix', reason='native AF_UNIX/SO_PEERCRED provider integration')
def test_native_policy_actual_socket_catalog_activation_profile_and_sqlite(native_policy_fixture, monkeypatch):
    import tempfile
    import eimemory.adapters.hermes.code_implementation as provider
    import eimemory.capabilities.code_implementation_bootstrap as bootstrap
    import eimemory.evaluation.hongtu_code_implementation as catalog_module
    from eimemory.evaluation.capability_catalog import ApplicationCatalogBootstrap, CapabilityEvaluationCatalog
    from eimemory.governance.capability.capability_incubation import execute_capability_incubation
    from eimemory.capabilities import CapabilityProfile
    f = native_policy_fixture
    scope = asdict(f.scope)
    monkeypatch.setattr(provider, 'resolve_code_implementation_provider', _REAL_PROVIDER_RESOLVER)
    monkeypatch.setattr(CapabilityProfiles, 'resolve', _REAL_PROFILE_RESOLVER)
    f.runtime.apply_capability_seed_manifest(scope=scope)
    f.grant['profile_key'] = 'l5.default'
    CapabilityProfiles(f.runtime.store).register(CapabilityProfile(
        profile_id='l5.default:fixture-native-owner', profile_key=f.grant['profile_key'], revision='fixture-native-owner', scope='global',
        requirements={'code.implementation': {'minimum_maturity': 'evaluated', 'min_pass_rate': 1.0}},
        provenance={'source': 'isolated-test-fixture-only'}, created_at=datetime.now(timezone.utc).isoformat()),
        runtime_scope=f.scope)
    # The actual provider transport, server attestation, static protected case,
    # native two-pass activation and resolver execute here. Only completion is
    # a deterministic fixture backend, with no production LLM or source access.
    def complete(**kwargs):
        request = json.loads(kwargs['input'][0]['text'])
        return SimpleNamespace(parsed={'schema': provider.RESPONSE_SCHEMA,
            'request_id': request['request_id'], 'request_digest': request['request_digest'],
            'file_updates': [{'path': item['path'], 'prior_sha256': item['sha256'], 'content': 'VALUE = 2\n'}
                             for item in request['allowed_files']],
            'rationale': 'isolated static provider fixture', 'assumptions': []})
    real_client = provider.CodeImplementationSocketClient
    with tempfile.TemporaryDirectory(prefix='eim-own-', dir='/var/tmp') as socket_dir:
        socket_path = Path(socket_dir)/'p.sock'
        def client(**kwargs): return real_client(socket_path=socket_path, **kwargs)
        monkeypatch.setattr(provider, 'CodeImplementationSocketClient', client)
        monkeypatch.setattr(bootstrap, 'CodeImplementationSocketClient', client)
        monkeypatch.setattr(bootstrap, 'CODE_IMPLEMENTATION_SOCKET', str(socket_path))
        monkeypatch.setattr(catalog_module, 'CodeImplementationSocketClient', client)
        monkeypatch.setenv('EIMEMORY_HERMES_GATEWAY_PROCESS', '1')
        server = provider.CodeImplementationSocketServer(SimpleNamespace(llm=SimpleNamespace(complete_structured=complete)), socket_path=socket_path)
        try:
            assert server.start() is True
            assert bootstrap.register_code_implementation_v2(f.runtime, runtime_scope=scope)['ok'] is True
            now = datetime.now(timezone.utc).replace(microsecond=0)
            advertisement = bootstrap.advertise_code_implementation_v2(f.runtime, runtime_scope=scope,
                advertised_at=now.isoformat(), expires_at=(now+timedelta(hours=1)).isoformat())
            assert advertisement['ok'] is True, advertisement
            catalog = CapabilityEvaluationCatalog()
            catalog_module.install_code_implementation_catalog(ApplicationCatalogBootstrap(catalog))
            catalog.seal()
            incubated = execute_capability_incubation(f.runtime, runtime_scope=scope, catalog=catalog,
                            max_candidates=100, max_activate=3, preflight_passes=2)
            assert incubated['ok'] is True and incubated['activated_count'] >= 1, incubated
            f.grant['profile_digest'] = CapabilityProfiles(f.runtime.store).resolve(f.grant['profile_key'],
                                  runtime_scope=f.scope, capability_scope='global')['profile']['profile_digest']
            resolved = _REAL_PROVIDER_RESOLVER(f.runtime, runtime_scope=scope, capability_scope='global',
                                               checked_at=datetime.now(timezone.utc).isoformat(), probe=True)
            assert resolved['ok'] is True and resolved['health']['ok'] is True, resolved
            f.capture()
            result = _ensure(f)
            assert result['ok'] is True and result['status'] == 'available', result
            assert json.loads(f.destination.read_bytes())['deployment']['observation_seconds'] == 28800
        finally:
            server.stop()


__all__ = ['native_policy_fixture'] + [name for name in globals() if name.startswith('test_')]
