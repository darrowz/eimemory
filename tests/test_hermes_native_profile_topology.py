"""Native multiplex proof rejects stale owners and missing logical profiles."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from eimemory.contracts.hermes_topology import canonical_bytes, topology_digest
from eimemory.ops import hermes_native_topology as native

COMMIT = 'a' * 40


def source_pins():
    release = '/opt/eimemory/releases/' + COMMIT
    return dict(source_commit=COMMIT, release_root=release,
        plugins=[dict(name=name, target=release + '/integrations/hermes/' + name,
                      manifest_sha256='2' * 64, entry_sha256='3' * 64)
                 for name in ('eimemory', 'eimemory_hook')])


def topology():
    body = dict(schema='eimemory.hermes_topology.v1', mode='single_native_gateway',
                runtime_uid=1001, runtime_gid=1001, owner_unit=native.MAIN_GATEWAY,
                runtime_units=['eimemory-rpc.service', native.MAIN_GATEWAY],
                retired_gateways=['hermes-gateway-team.service'], profiles=[
                    dict(profile_name='default', hermes_home='/srv/profiles/default', agent_id='primary',
                         identity_status='bound', capture_scopes=[dict(scope=dict(tenant_id='t',
                              agent_id='primary', workspace_id='w', user_id='u'), channel='hermes', source_ids=['hermes'])]),
                    dict(profile_name='team', hermes_home='/srv/profiles/team', agent_id='team',
                         identity_status='unbound', capture_scopes=[])])
    return dict(body, topology_digest=topology_digest(body))


def native_fixture(monkeypatch):
    now = datetime.now(timezone.utc)
    state = dict(pid=1, start_time=4200, hermes_home='/srv/profiles/default',
                 gateway_state='running', updated_at=now.isoformat(), served_profiles=['team', 'default'])
    owner = dict(unit=native.MAIN_GATEWAY, pid=1234, start_ticks=4200, invocation_id='a' * 32)
    observation = dict(namespace_pids=[1234, 1], environment={'HERMES_HOME': '/srv/profiles/default'})
    monkeypatch.setattr(native, '_process_owner', lambda: (deepcopy(owner), deepcopy(observation)))
    monkeypatch.setattr(native, '_release_source_pins', lambda expected_commit: source_pins())
    monkeypatch.setattr(native, '_read', lambda path, **kw: json.dumps(state).encode())
    def config(profile, pins):
        return dict(profile_name=profile['profile_name'], hermes_home=profile['hermes_home'],
                    ok=True, reason='', config_sha256='1' * 64, provider='eimemory', hook_enabled=True,
                    plugin_links=deepcopy(pins['plugins']))
    monkeypatch.setattr(native, '_configuration', config)
    return state, owner, observation


def test_single_owner_nested_pid_namespace_proves_all_profiles(monkeypatch):
    native_fixture(monkeypatch)
    proof = native.collect_native_profile_topology(topology(), {'MainPID': '1234'}, expected_commit=COMMIT)
    assert native.validate_native_profile_topology(proof, topology(), expected_commit=COMMIT) == proof
    assert proof['served_profiles'] == ['default', 'team']
    # Identity qualification is separate: an unbound served profile is reported
    # truthfully, never relabeled as a qualified business sample by this probe.
    assert topology()['profiles'][1]['identity_status'] == 'unbound'


@pytest.mark.parametrize('mutation', [
    lambda state: state.update(pid=9),
    lambda state: state.update(start_time=4199),
    lambda state: state.update(gateway_state='stopped'),
    lambda state: state.update(served_profiles=['default']),
    lambda state: state.update(served_profiles=['default', 'team', 'unknown']),
    lambda state: state.update(updated_at=(datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()),
])
def test_stale_or_partial_native_observation_cannot_pass(monkeypatch, mutation):
    state, _, _ = native_fixture(monkeypatch)
    mutation(state)
    proof = native.collect_native_profile_topology(topology(), expected_commit=COMMIT)
    assert proof['complete'] is False
    with pytest.raises(ValueError):
        native.validate_native_profile_topology(proof, topology(), expected_commit=COMMIT)


def test_gateway_restart_during_observation_is_rejected(monkeypatch):
    _, owner, observation = native_fixture(monkeypatch)
    calls = iter([(owner, observation), (dict(owner, start_ticks=4201), observation)])
    monkeypatch.setattr(native, '_process_owner', lambda: next(calls))
    assert native.collect_native_profile_topology(topology(), expected_commit=COMMIT)['complete'] is False


def test_missing_profile_plugin_is_not_hidden_by_main_gateway_health(monkeypatch):
    native_fixture(monkeypatch)
    original = native._configuration
    def missing(profile, pins):
        if profile['profile_name'] == 'team':
            raise FileNotFoundError('plugin missing')
        return original(profile, pins)
    monkeypatch.setattr(native, '_configuration', missing)
    proof = native.collect_native_profile_topology(topology(), expected_commit=COMMIT)
    assert proof['complete'] is False
    assert proof['profile_configs'][1]['ok'] is False


def test_sealed_replay_cannot_drop_profile_or_change_metadata(monkeypatch):
    native_fixture(monkeypatch)
    proof = native.collect_native_profile_topology(topology(), expected_commit=COMMIT)
    proof['profile_configs'][1]['config_sha256'] = '4' * 64
    with pytest.raises(ValueError, match='proof_invalid'):
        native.validate_native_profile_topology(proof, topology(), expected_commit=COMMIT)


def test_invalid_plugin_hash_cannot_pass_even_with_recomputed_digest(monkeypatch):
    native_fixture(monkeypatch)
    proof = native.collect_native_profile_topology(topology(), expected_commit=COMMIT)
    proof['profile_configs'][1]['plugin_links'][0]['entry_sha256'] = 'invalid'
    proof['stable_digest'] = sha256(canonical_bytes(native._stable_body(proof))).hexdigest()
    with pytest.raises(ValueError, match='plugin_metadata'):
        native.validate_native_profile_topology(proof, topology(), expected_commit=COMMIT)


@pytest.mark.parametrize('field,value', [
    ('target', '/opt/eimemory/releases/' + 'b' * 40 + '/integrations/hermes/eimemory'),
    ('entry_sha256', '4' * 64), ('manifest_sha256', '5' * 64),
])
def test_historical_replay_rejects_another_profile_plugin_source_even_with_new_stable_digest(monkeypatch,field,value):
    native_fixture(monkeypatch)
    proof=native.collect_native_profile_topology(topology(),expected_commit=COMMIT)
    proof['profile_configs'][1]['plugin_links'][0][field]=value
    proof['stable_digest']=sha256(canonical_bytes(native._stable_body(proof))).hexdigest()
    with pytest.raises(ValueError,match='plugin_source_mismatch'):
        native.validate_native_profile_topology(proof,topology(),expected_commit=COMMIT)


def test_historical_replay_uses_sealed_expected_pins_without_reading_current_files(monkeypatch):
    native_fixture(monkeypatch)
    proof=native.collect_native_profile_topology(topology(),expected_commit=COMMIT)
    monkeypatch.setattr(native,'_read',lambda *a,**kw:pytest.fail('historical replay must not read host files'))
    monkeypatch.setattr(native,'_release_source_pins',lambda *a,**kw:pytest.fail('historical replay must not resolve current release'))
    assert native.validate_native_profile_topology(proof,topology(),expected_commit=COMMIT)==proof
    with pytest.raises(ValueError,match='source_pins_invalid'):
        native.validate_native_profile_topology(proof,topology(),expected_commit='b'*40)


@pytest.mark.skipif(os.name!='posix',reason='Immutable native POSIX release paths')
def test_collector_source_pins_come_from_its_actual_immutable_release(monkeypatch):
    release=Path('/opt/eimemory/releases')/COMMIT
    monkeypatch.setattr(native,'__file__',str(release/'eimemory/ops/hermes_native_topology.py'))
    reads=[]
    def read(path):
        reads.append(path)
        assert path.is_relative_to(release/'integrations/hermes')
        return path.as_posix().encode()
    monkeypatch.setattr(native,'_read',read)
    pins=native._release_source_pins(COMMIT)
    assert pins['source_commit']==COMMIT and len(reads)==4
    assert all(row['entry_sha256']==sha256((release/'integrations/hermes'/row['name']/'__init__.py').as_posix().encode()).hexdigest()
               for row in pins['plugins'])
    with pytest.raises(ValueError,match='collector_release_mismatch'):
        native._release_source_pins('b'*40)


@pytest.mark.skipif(os.name!='posix',reason='Native POSIX safe-file reader and symlinks')
def test_current_native_configuration_compares_real_plugin_bytes_with_release_pins(monkeypatch,tmp_path):
    # Only the immutable-release resolver is stubbed here; native plugin
    # symlinks, manifests and entry bytes are read through the real safe reader.
    release=tmp_path/'release'
    plugins=[]
    for name in ('eimemory','eimemory_hook'):
        target=release/'integrations/hermes'/name
        target.mkdir(parents=True)
        (target/'plugin.yaml').write_text('name: '+name+'\n')
        (target/'__init__.py').write_text('# '+name+' entry\n')
        plugins.append(dict(name=name,target=str(target),
            manifest_sha256=sha256((target/'plugin.yaml').read_bytes()).hexdigest(),
            entry_sha256=sha256((target/'__init__.py').read_bytes()).hexdigest()))
    home=tmp_path/'profile'
    (home/'plugins').mkdir(parents=True)
    (home/'config.yaml').write_text('memory:\n  provider: eimemory\nplugins:\n  enabled: [eimemory-hook]\n')
    for plugin in plugins:(home/'plugins'/plugin['name']).symlink_to(plugin['target'],target_is_directory=True)
    pins=dict(source_commit=COMMIT,release_root=str(release),plugins=plugins)
    profile=dict(profile_name='team',hermes_home=str(home))
    assert native._configuration(profile,pins)['ok']
    (release/'integrations/hermes/eimemory/__init__.py').write_text('# stale or replaced plugin\n')
    result=native._configuration(profile,pins)
    assert not result['ok'] and result['reason']=='hermes_native_plugin_source_mismatch'


def test_source_pin_failure_blocks_collection_before_runtime_or_native_reads(monkeypatch):
    def mismatch(expected_commit):raise ValueError('hermes_native_collector_release_mismatch')
    monkeypatch.setattr(native,'_release_source_pins',mismatch)
    monkeypatch.setattr(native,'_process_owner',lambda:pytest.fail('source mismatch must fail first'))
    proof=native.collect_native_profile_topology(topology(),expected_commit=COMMIT)
    assert not proof['complete']
    assert proof['blocking_reasons']==['hermes_native_collector_release_mismatch']
