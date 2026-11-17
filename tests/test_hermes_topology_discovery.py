"""Protected logical declarations do not enroll a runtime-manager transport."""
import os
import builtins
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from eimemory.contracts import hermes_topology as topology
from eimemory.contracts import user_control_plane as authority
from eimemory.ops import runtime_service_observation as observation

_NATIVE_AUTHORITY = topology._authority


def _documents(uid, gid, operator):
    body = dict(schema=topology.SCHEMA, mode='single_native_gateway',
        runtime_uid=uid, runtime_gid=gid, owner_unit=topology.MAIN_GATEWAY,
        runtime_units=list(topology.RUNTIME_UNITS), retired_gateways=['hermes-gateway-retired.service'],
        profiles=[dict(profile_name='default', hermes_home='/srv/profiles/main', agent_id='alpha',
            identity_status='bound', capture_scopes=[dict(channel='hermes', source_ids=['hermes'],
                scope=dict(tenant_id='tenant', agent_id='alpha', workspace_id='project', user_id='business-user'))],
            operator_scope=dict(tenant_id='tenant', agent_id='alpha', workspace_id='project', user_id=operator))])
    intent = dict(schema='eimemory.hermes_topology_intent.v1', intent_id='a'*32,
        runtime_uid=uid, runtime_gid=gid, mode='single_native_gateway', main_gateway=topology.MAIN_GATEWAY,
        retired_gateways=body['retired_gateways'], topology_sha256=topology.topology_digest(body),
        native_snapshot_sha256='b'*64, authorization='explicit_user_operator', autonomous_repair_credit=False,
        source={key: 'c'*(32 if key == 'invocation_id' else 40 if key == 'base_commit' else 64)
            for key in ('registry_sha256','transition_sha256','job_key','job_sha256',
                        'invocation_id','base_commit','capture_sha256')})
    return body, intent


def _write_pair(home, uid, gid, operator):
    directory = home / '.config/eimemory/control-plane'
    directory.mkdir(parents=True)
    body, intent = _documents(uid, gid, operator)
    logical, pin = directory / topology.TOPOLOGY_NAME, directory / topology.INTENT_NAME
    logical.write_bytes(topology.canonical_bytes(body))
    pin.write_bytes(topology.canonical_bytes(intent))
    for path in (logical, pin):
        path.chmod(0o640)
    return SimpleNamespace(home=home, directory=directory, body=body, intent=intent, logical=logical, pin=pin)


@pytest.fixture
def declared_account(tmp_path, monkeypatch):
    """Model the OS account on every platform; exercise real discovery/parser.

    Filesystem permission enforcement has separate POSIX tests below. This
    fixture replaces only the OS-account/cgroup and secure-reader boundaries.
    """
    # The global suite fence disables topology discovery. Restore it only after
    # providing this test's synthetic OS account and filesystem boundary.
    monkeypatch.setattr(topology, '_authority', _NATIVE_AUTHORITY)
    home = tmp_path / 'real-account'
    native = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name='operator', pw_dir=str(home))
    fake_os = SimpleNamespace(name='posix', environ=os.environ, path=os.path,
        geteuid=lambda: 1001, getuid=lambda: 1001, getegid=lambda: 1001, getgid=lambda: 1001)
    monkeypatch.setattr(topology, 'os', fake_os)
    monkeypatch.setattr(authority, 'os', fake_os)
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwuid=lambda uid: native))
    monkeypatch.delenv('EIMEMORY_CONTROL_PLANE', raising=False)
    monkeypatch.delenv('EIMEMORY_RUNTIME_TRANSITION_JOB', raising=False)
    context = SimpleNamespace(cgroup='0::/user.slice/user-1001.slice/session-17.scope\n')
    monkeypatch.setattr(observation, 'Path', lambda path: SimpleNamespace(read_text=lambda: context.cgroup))
    pair = _write_pair(home, 1001, 1001, 'operator')
    pair.account, pair.os, pair.context, pair.reads = native, fake_os, context, []
    def reader(path, **kwargs):
        pair.reads.append(Path(path))
        assert Path(path).parent == pair.directory
        return Path(path).read_bytes()
    monkeypatch.setattr(authority, 'read_file', reader)
    from eimemory.core import runtime_manager
    monkeypatch.setattr(runtime_manager, 'request', lambda *a, **kw: pytest.fail('discovery must not call a manager'))
    return pair


def test_ordinary_account_discovers_protected_pair_without_enrolling_transport(declared_account, monkeypatch):
    pair = declared_account
    monkeypatch.setenv('HOME', str(pair.home / 'spoofed'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(pair.home / 'other-authority'))
    before = {path: path.read_bytes() for path in pair.directory.iterdir()}
    assert authority.user_mode() is False
    assert observation.registered_runtime_context() is False
    result = topology.load_verified_topology()
    assert result['topology_digest'] == topology.topology_digest(pair.body)
    assert pair.reads == [pair.logical, pair.pin, pair.logical, pair.pin]
    assert {path: path.read_bytes() for path in pair.directory.iterdir()} == before
    assert authority.user_mode() is False
    assert observation.registered_runtime_context() is False
    assert 'EIMEMORY_CONTROL_PLANE' not in os.environ


def test_explicit_user_mode_keeps_existing_authority_route(declared_account, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CONTROL_PLANE', 'user')
    declared_account.os.getgid = lambda: 1002  # Do not change the existing managed account contract.
    monkeypatch.setattr(observation, 'registered_runtime_context',
        lambda: pytest.fail('existing user authority must not consult the unmanaged route'))
    assert topology.load_verified_topology()['topology_digest'] == topology.topology_digest(declared_account.body)


@pytest.mark.parametrize('context', ['transition', 'pid1'])
def test_legacy_managed_context_cannot_switch_to_user_declaration(declared_account, monkeypatch, context):
    if context == 'transition':
        monkeypatch.setenv('EIMEMORY_RUNTIME_TRANSITION_JOB', 'd'*64)
    else:
        declared_account.context.cgroup = '0::/system.slice/eimemory-acceptance.service\n'
    assert observation.registered_runtime_context() is True
    assert topology.load_topology() is None
    assert declared_account.reads == []


def test_invalid_transition_identity_is_not_silently_unmanaged(declared_account, monkeypatch):
    monkeypatch.setenv('EIMEMORY_RUNTIME_TRANSITION_JOB', 'not-a-native-job')
    with pytest.raises(ValueError, match='runtime_transition_job_invalid'):
        topology.load_topology()
    assert declared_account.reads == []


def test_invalid_control_plane_selector_still_fails_closed(declared_account, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CONTROL_PLANE', 'other')
    with pytest.raises(ValueError, match='user_control_plane_mode_invalid'):
        topology.load_topology()


def test_non_posix_unconfigured_consumers_do_not_require_posix_account(declared_account, monkeypatch):
    declared_account.os.name = 'nt'
    monkeypatch.setattr(authority, 'account', lambda: pytest.fail('non-POSIX discovery must remain absent'))
    assert topology.load_topology() is None
    assert declared_account.reads == []


def test_root_cannot_discover_a_user_pair(declared_account, monkeypatch):
    declared_account.os.geteuid = lambda: 0
    monkeypatch.setattr(authority, 'account', lambda: pytest.fail('root must not choose a user authority'))
    assert topology.load_topology() is None
    assert declared_account.reads == []


def test_setgid_operator_cannot_gain_ordinary_user_authority(declared_account):
    declared_account.os.getgid = lambda: 1002
    with pytest.raises(ValueError, match='hermes_topology_native_group_identity_invalid'):
        topology.load_topology()
    assert declared_account.reads == []


@pytest.mark.parametrize('fault', ['setuid', 'primary_group', 'unknown_account', 'relative_home'])
def test_invalid_native_account_cannot_discover_a_pair(declared_account, monkeypatch, fault):
    pair = declared_account
    if fault == 'setuid':
        pair.os.getuid = lambda: 1002
    elif fault == 'primary_group':
        pair.os.getegid = lambda: 1002
    elif fault == 'unknown_account':
        def missing(uid):
            raise KeyError(uid)
        monkeypatch.setattr(sys.modules['pwd'], 'getpwuid', missing)
    else:
        pair.account.pw_dir = 'relative/home'
    with pytest.raises((ValueError, KeyError)):
        topology.load_topology()
    assert pair.reads == []


def test_absent_pair_is_unconfigured_but_partial_pair_is_not(declared_account):
    pair = declared_account
    pair.pin.unlink()
    with pytest.raises(ValueError, match='registration_incomplete'):
        topology.load_topology()
    pair.logical.unlink()
    assert topology.load_topology() is None


@pytest.mark.parametrize('fault', ['intent_digest', 'intent_uid', 'autonomous_credit', 'source_shape',
                                   'body_gid', 'operator_identity', 'duplicate_json', 'malformed_json'])
def test_discovered_pair_retains_all_existing_identity_and_intent_guards(declared_account, fault):
    pair = declared_account
    if fault == 'intent_digest':
        pair.intent['topology_sha256'] = '0'*64
    elif fault == 'intent_uid':
        pair.intent['runtime_uid'] = 1002
    elif fault == 'autonomous_credit':
        pair.intent['autonomous_repair_credit'] = True
    elif fault == 'source_shape':
        pair.intent['source']['registry_sha256'] = 'invalid'
    elif fault == 'body_gid':
        pair.body['runtime_gid'] = 1002
        pair.intent['topology_sha256'] = topology.topology_digest(pair.body)
    elif fault == 'operator_identity':
        pair.body['profiles'][0]['operator_scope']['user_id'] = 'foreign-operator'
        pair.intent['topology_sha256'] = topology.topology_digest(pair.body)
    pair.logical.write_bytes(topology.canonical_bytes(pair.body))
    pair.pin.write_bytes(topology.canonical_bytes(pair.intent))
    if fault == 'duplicate_json':
        pair.logical.write_bytes(b'{"schema":"duplicate",' + pair.logical.read_bytes()[1:])
    elif fault == 'malformed_json':
        pair.logical.write_bytes(b'{invalid')
    with pytest.raises(ValueError, match='hermes_topology_(registration|operator_binding)'):
        topology.load_topology()


def test_discovery_does_not_mask_protected_reader_failure(declared_account, monkeypatch):
    def reject(path, **kwargs):
        raise ValueError('user_control_plane_file_untrusted')
    monkeypatch.setattr(authority, 'read_file', reject)
    with pytest.raises(ValueError, match='user_control_plane_file_untrusted'):
        topology.load_topology()


def test_discovery_rechecks_the_pair_after_validation(declared_account, monkeypatch):
    pair = declared_account
    calls = []
    def replacing_reader(path, **kwargs):
        calls.append(path)
        if len(calls) == 4:
            return Path(path).read_bytes() + b'\n'
        return Path(path).read_bytes()
    monkeypatch.setattr(authority, 'read_file', replacing_reader)
    with pytest.raises(ValueError, match='registration_changed'):
        topology.load_topology()


def test_discovered_operator_never_substitutes_for_business_scope(declared_account):
    from eimemory.evaluation.hermes_profile_coverage import policy_covers_topology
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    value = topology.load_verified_topology()
    profile = value['profiles'][0]
    policy = {'agents': [{'scope': resolve_channel_scope('hermes', profile['operator_scope']), 'source_ids': ['hermes']}]}
    with pytest.raises(ValueError, match='hermes_business_scope_inventory_mismatch'):
        policy_covers_topology(policy, value)
    policy['agents'][0]['scope'] = resolve_channel_scope('hermes', profile['capture_scopes'][0]['scope'])
    assert policy_covers_topology(policy, value) is True


def test_copied_contract_validation_remains_standalone(monkeypatch):
    """Controller copies validate declarations without an installed package."""
    body, intent = _documents(1001, 1001, 'operator')
    native_import = builtins.__import__
    def isolated_import(name, *args, **kwargs):
        if name == 'eimemory' or name.startswith('eimemory.'):
            raise AssertionError('standalone declaration validation imported the product')
        return native_import(name, *args, **kwargs)
    specification = importlib.util.spec_from_file_location('copied_hermes_topology_contract', topology.__file__)
    copied = importlib.util.module_from_spec(specification)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', isolated_import)
        specification.loader.exec_module(copied)
        assert copied.validate_topology(body) == body
        assert copied.validate_operator_topology_intent(intent, main=copied.MAIN_GATEWAY,
            retired=body['retired_gateways'], uid=1001, gid=1001) == intent


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: 0)() == 0,
                    reason='real unprivileged POSIX authority reader')
@pytest.mark.parametrize('fault', ['none', 'final_symlink', 'ancestor_symlink', 'hardlink', 'writable_file', 'writable_parent'])
def test_ordinary_discovery_uses_real_secure_reader(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(topology, '_authority', _NATIVE_AUTHORITY)
    native = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_name='operator', pw_dir=str(tmp_path/'account'))
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwuid=lambda uid: native))
    monkeypatch.delenv('EIMEMORY_CONTROL_PLANE', raising=False)
    monkeypatch.delenv('EIMEMORY_RUNTIME_TRANSITION_JOB', raising=False)
    monkeypatch.setattr(observation, 'Path', lambda path: SimpleNamespace(read_text=lambda: '0::/user.slice/session.scope\n'))
    pair = _write_pair(Path(native.pw_dir), native.pw_uid, native.pw_gid, native.pw_name)
    if fault == 'final_symlink':
        original = pair.directory/'real.json'
        pair.logical.rename(original)
        pair.logical.symlink_to(original)
    elif fault == 'ancestor_symlink':
        original = pair.directory.with_name('original')
        pair.directory.rename(original)
        pair.directory.symlink_to(original, target_is_directory=True)
    elif fault == 'hardlink':
        os.link(pair.logical, pair.directory/'alias.json')
    elif fault == 'writable_file':
        pair.logical.chmod(0o666)
    elif fault == 'writable_parent':
        pair.directory.chmod(0o777)
    if fault == 'none':
        assert topology.load_verified_topology()['topology_digest'] == topology.topology_digest(pair.body)
    else:
        with pytest.raises((OSError, ValueError)):
            topology.load_topology()
