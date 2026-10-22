"""Exact profile identity survives multiplexing without weakening scope gates."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from eimemory.contracts import hermes_topology as contract


def declaration():
    return dict(schema=contract.SCHEMA, mode='single_native_gateway', runtime_uid=1001,
                runtime_gid=1001, owner_unit=contract.MAIN_GATEWAY,
                runtime_units=list(contract.RUNTIME_UNITS), retired_gateways=['hermes-gateway-team.service'],
                profiles=[dict(profile_name='default', hermes_home='/srv/runtime/primary',
                               agent_id='primary', identity_status='bound', capture_scopes=[dict(
                                   scope=dict(tenant_id='tenant', agent_id='primary',
                                              workspace_id='workspace', user_id='caller'),
                                   channel='hermes', source_ids=['hermes'])]),
                          dict(profile_name='team', hermes_home='/srv/runtime/team',
                               agent_id='team', identity_status='unbound', capture_scopes=[])])


def snapshot(body=None):
    body = declaration() if body is None else body
    return dict(body, topology_digest=contract.topology_digest(body))


def test_exact_native_profile_home_and_user_are_required():
    result = contract.resolve_profile_binding('default', '/srv/runtime/primary',
                                              user_id='caller', topology=snapshot())
    assert result['scope']['agent_id'] == 'primary'
    for profile, home, user, reason in [
        ('team', '/srv/runtime/team', 'caller', 'identity_unbound'),
        ('default', '/srv/runtime/team', 'caller', 'profile_unregistered'),
        ('default', '/srv/runtime/primary', 'stranger', 'scope_unbound'),
    ]:
        with pytest.raises(ValueError, match=reason):
            contract.resolve_profile_binding(profile, home, user_id=user, topology=snapshot())


def test_multiple_real_users_and_workspaces_remain_independent():
    body = declaration()
    second = deepcopy(body['profiles'][0]['capture_scopes'][0])
    second['scope']['workspace_id'] = 'second'
    body['profiles'][0]['capture_scopes'].append(second)
    with pytest.raises(ValueError, match='ambiguous'):
        contract.resolve_profile_binding('default', '/srv/runtime/primary', user_id='caller',
                                         topology=snapshot(body))
    selected = contract.resolve_profile_binding('default', '/srv/runtime/primary', user_id='caller',
                                                workspace_id='second', topology=snapshot(body))
    assert selected['scope']['workspace_id'] == 'second'


def test_snapshot_cannot_relabel_profile_after_digest_binding():
    value = snapshot()
    value['profiles'][0]['agent_id'] = 'other'
    value['profiles'][0]['capture_scopes'][0]['scope']['agent_id'] = 'other'
    with pytest.raises(ValueError, match='digest_mismatch'):
        contract.validate_snapshot(value)


@pytest.mark.parametrize('mutate', [
    lambda body: body.update(runtime_uid=0),
    lambda body: body.update(runtime_gid=True),
    lambda body: body['runtime_units'].append('hermes-gateway-team.service'),
    lambda body: body['retired_gateways'].append(contract.MAIN_GATEWAY),
    lambda body: body['profiles'][0].update(hermes_home='/srv/../runtime'),
    lambda body: body['profiles'][1].update(identity_status='bound'),
    lambda body: body['profiles'][0]['capture_scopes'][0].update(channel='other'),
    lambda body: body['profiles'][0]['capture_scopes'][0]['scope'].update(user_id=''),
    lambda body: body['profiles'].append(deepcopy(body['profiles'][0])),
])
def test_malformed_or_conflicting_declaration_is_rejected(mutate):
    body = declaration()
    mutate(body)
    with pytest.raises(ValueError):
        contract.validate_topology(body)


def test_validated_value_has_no_mutable_input_alias():
    body = declaration()
    checked = contract.validate_topology(body)
    body['profiles'][0]['capture_scopes'][0]['scope']['user_id'] = 'changed'
    assert checked['profiles'][0]['capture_scopes'][0]['scope']['user_id'] == 'caller'


def registered_files(tmp_path, monkeypatch):
    body = declaration()
    intent = dict(schema='eimemory.hermes_topology_intent.v1', mode='single_native_gateway',
                  main_gateway=contract.MAIN_GATEWAY, authorization='explicit_user_operator',
                  autonomous_repair_credit=False, runtime_uid=1001, runtime_gid=1001,
                  retired_gateways=body['retired_gateways'], topology_sha256=contract.topology_digest(body),
                  intent_id='a'*32, native_snapshot_sha256='b'*64,
                  source={key: 'c'*(32 if key == 'invocation_id' else 40 if key == 'base_commit' else 64)
                          for key in ('registry_sha256', 'transition_sha256', 'job_key', 'job_sha256',
                                      'invocation_id', 'base_commit', 'capture_sha256')})
    monkeypatch.setattr(contract, '_authority', lambda: (
        dict(authority=tmp_path, uid=1001, gid=1001), lambda path, **kw: Path(path).read_bytes()))
    logical = tmp_path / contract.TOPOLOGY_NAME
    pin = tmp_path / contract.INTENT_NAME
    logical.write_bytes(contract.canonical_bytes(body))
    pin.write_bytes(contract.canonical_bytes(intent))
    return body, intent, logical, pin


def test_only_absent_enrollment_may_use_legacy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, '_authority', lambda: (
        dict(authority=tmp_path, uid=1001, gid=1001), lambda path, **kw: Path(path).read_bytes()))
    assert contract.load_topology() is None
    with pytest.raises(ValueError, match='not_registered'):
        contract.load_verified_topology()
    (tmp_path / contract.TOPOLOGY_NAME).write_text('{}')
    with pytest.raises(ValueError, match='incomplete'):
        contract.load_topology()


def test_registered_digest_and_runtime_identity_are_mandatory(tmp_path, monkeypatch):
    body, intent, logical, pin = registered_files(tmp_path, monkeypatch)
    assert contract.load_verified_topology() == snapshot(body)
    intent['topology_sha256'] = '0' * 64
    pin.write_bytes(contract.canonical_bytes(intent))
    with pytest.raises(ValueError, match='operator_binding'):
        contract.load_topology()
    intent['topology_sha256'] = contract.topology_digest(body)
    intent['runtime_uid'] = 1002
    pin.write_bytes(contract.canonical_bytes(intent))
    with pytest.raises(ValueError, match='registration_invalid'):
        contract.load_topology()


def test_corrupt_enrollment_does_not_silently_become_unregistered(tmp_path, monkeypatch):
    _, _, logical, _ = registered_files(tmp_path, monkeypatch)
    logical.write_text('{invalid')
    with pytest.raises(ValueError, match='registration_invalid'):
        contract.load_topology()


def test_snapshot_replay_is_pure_and_does_not_read_current_authority(monkeypatch):
    monkeypatch.setattr(contract, '_authority', lambda: pytest.fail('historical replay must be pure'))
    assert contract.validate_snapshot(snapshot()) == snapshot()


def test_native_local_operator_is_independent_of_business_capture_authority():
    body = declaration()
    operator = dict(tenant_id='tenant', agent_id='team', workspace_id='workspace', user_id='local-owner')
    body['profiles'][1]['operator_scope'] = operator
    topo = snapshot(body)
    explicit = contract.resolve_profile_binding('team', '/srv/runtime/team', user_id='local-owner', topology=topo)
    assert explicit['scope'] == operator
    assert explicit['binding_kind'] == 'operator'
    assert explicit['natural_sample_eligible'] is False
    # Existence of local operator access does not authorize an external user.
    with pytest.raises(ValueError, match='identity_unbound'):
        contract.resolve_profile_binding('team', '/srv/runtime/team', user_id='external', topology=topo)
    assert body['profiles'][1]['capture_scopes'] == []


def test_operator_binding_cannot_impersonate_another_agent():
    body = declaration()
    body['profiles'][1]['operator_scope'] = dict(tenant_id='tenant', agent_id='primary', workspace_id='workspace', user_id='local-owner')
    with pytest.raises(ValueError, match='operator_scope_profile_mismatch'):
        contract.validate_topology(body)


def test_loader_rejects_an_operator_scope_for_a_different_os_account(tmp_path, monkeypatch):
    body, intent, logical, pin = registered_files(tmp_path, monkeypatch)
    body['profiles'][0]['operator_scope'] = dict(tenant_id='tenant', agent_id='primary',
                                                workspace_id='workspace', user_id='foreign-owner')
    intent['topology_sha256'] = contract.topology_digest(body)
    logical.write_bytes(contract.canonical_bytes(body))
    pin.write_bytes(contract.canonical_bytes(intent))
    with pytest.raises(ValueError, match='operator_binding'):
        contract.load_topology()


def test_topology_evidence_cannot_be_modified_by_an_autonomous_patch():
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    for path in ('eimemory/contracts/hermes_topology.py', 'eimemory/ops/hermes_native_topology.py',
                 'eimemory/evaluation/hermes_profile_coverage.py'):
        assert path_denied_by_self(path)
