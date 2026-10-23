"""One physical owner, five independently authorized logical colleagues."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.contracts.hermes_topology import topology_digest, canonical_bytes
from eimemory.evaluation import hermes_profile_coverage as coverage
from eimemory.evaluation import recall_agent_scopes as agents
from eimemory.ops import controlled_stability as stability
from eimemory.ops import hermes_native_topology as native
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_controlled_stability import samples, seal, COMMIT
from test_controlled_production_acceptance import setup, OWNER, SCOPES


def topology(*, unbound=False):
    names = ['default', 'audit-a', 'audit-b', 'audit-c', 'audit-d']
    body = {'schema': 'eimemory.hermes_topology.v1', 'mode': 'single_native_gateway',
        'runtime_uid': 1001, 'runtime_gid': 1001, 'owner_unit': 'hermes-gateway.service',
        'runtime_units': ['eimemory-rpc.service', 'hermes-gateway.service'],
        'retired_gateways': ['hermes-gateway-old-a.service', 'hermes-gateway-old-b.service'],
        'profiles': [{'profile_name': name, 'hermes_home': '/home/audit/.hermes' +
                     ('' if name == 'default' else '/profiles/' + name),
            'agent_id': name, 'identity_status': 'unbound' if unbound and n == 4 else 'bound',
            'capture_scopes': [] if unbound and n == 4 else [{'scope': {
                'tenant_id': 'audit-tenant', 'agent_id': name, 'workspace_id': 'office::channel::hermes',
                'user_id': 'member-' + str(n)}, 'channel': 'hermes', 'source_ids': ['hermes']}]}
            for n, name in enumerate(names)]}
    return {**body, 'topology_digest': topology_digest(body)}


def declared(monkeypatch, value):
    from eimemory.contracts import hermes_topology
    monkeypatch.setattr(hermes_topology, 'load_topology', lambda: deepcopy(value))
    monkeypatch.setattr(hermes_topology, 'load_verified_topology', lambda: deepcopy(value))


def operator_topology():
    value = topology()
    value.pop('topology_digest')
    value['profiles'][0]['operator_scope'] = {'tenant_id': 'audit-tenant', 'agent_id': 'default',
        'workspace_id': 'office::channel::hermes', 'user_id': 'local-owner'}
    value['topology_digest'] = topology_digest(value)
    return value


def decision(sqlite, scope, *, key, acceptance=0, session='native-session', source='hermes'):
    cols = {row[1]: row for row in sqlite.execute('PRAGMA table_info(proactive_decisions)')}
    values = {}
    for name, row in cols.items():
        if row[3] and row[4] is None:
            values[name] = 0 if 'INT' in str(row[2]).upper() or 'REAL' in str(row[2]).upper() else ''
    values.update(scope, decision_id=key, channel='hermes', source_ids_json=json.dumps([source]),
        session_id=session, acceptance_generated=acceptance,
        created_at=datetime.now(timezone.utc).isoformat(), updated_at=datetime.now(timezone.utc).isoformat())
    sqlite.execute('INSERT INTO proactive_decisions (' + ','.join(values) + ') VALUES ('
        + ','.join('?' for _ in values) + ')', tuple(values.values()))
    sqlite.conn.commit()


def test_five_scopes_need_one_owner_and_no_six_entry_floor():
    value = topology()
    allowed = coverage.required_capture_scopes(value)
    assert len(allowed) == 5
    assert coverage.capture_coverage(allowed, value)['ok']
    summary = coverage.roster_summary(value)
    assert summary['physical_gateway_count'] == 1 and summary['logical_profile_count'] == 5
    assert summary['runtime_units'] == ['eimemory-rpc.service', 'hermes-gateway.service']


@pytest.mark.parametrize('field', coverage.CAPTURE_FIELDS)
def test_right_agent_wrong_exact_capture_coordinate_never_passes(field):
    value = topology()
    allowed = coverage.required_capture_scopes(value)
    allowed[-1][field] = 'wrong-partition'
    verdict = coverage.capture_coverage(allowed, value)
    assert verdict['ok'] is False and verdict['missing_scope_count'] == 1


def test_duplicates_and_wildcards_cannot_substitute_for_missing_scope():
    value = topology()
    rows = coverage.required_capture_scopes(value)
    assert not coverage.capture_coverage(rows + [rows[0]], value)['ok']
    rows[-1]['user_id'] = '*'
    assert not coverage.capture_coverage(rows, value)['ok']


def test_business_policy_must_cover_every_exact_profile_without_inventing_unbound_identity():
    value = topology()
    policy = {'agents': [{'scope': row['scope'], 'source_ids': row['source_ids']}
                        for row in coverage.profile_bindings(value)]}
    assert coverage.policy_covers_topology(policy, value)
    with pytest.raises(ValueError, match='business_scope_inventory_mismatch'):
        coverage.policy_covers_topology({'agents': policy['agents'][:-1]}, value)
    # An operator-only profile does not expand the protected business roster.
    assert coverage.policy_covers_topology({'agents': policy['agents'][:-1]}, topology(unbound=True))
    with pytest.raises(ValueError, match='business_scope_inventory_mismatch'):
        coverage.policy_covers_topology(policy, topology(unbound=True))


def test_concurrent_five_profiles_keep_exact_scopes_and_exclude_synthetic_or_foreign_traffic(tmp_path, monkeypatch):
    value = topology(); declared(monkeypatch, value)
    with Runtime.create(root=tmp_path) as runtime:
        bindings = coverage.profile_bindings(value)
        def write(n):
            scope = bindings[n]['scope']
            runtime.store.run_locked(lambda db: decision(db, scope, key='native-' + str(n)))
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write, range(5)))
        runtime.store.run_locked(lambda db: decision(db, {**bindings[0]['scope'], 'user_id': 'forged-user'}, key='foreign'))
        runtime.store.run_locked(lambda db: decision(db, bindings[0]['scope'], key='maintenance', acceptance=1))
        runtime.store.run_locked(lambda db: decision(db, bindings[0]['scope'], key='release', session='hermes-release-test'))
        runtime.store.run_locked(lambda db: decision(db, bindings[0]['scope'], key='wrong-source', source='foreign-source'))
        scopes = agents.recall_agent_scopes(runtime, {'tenant_id': 'audit-tenant'})
        assert len(scopes) == 5 and [row['discovered_decisions'] for row in scopes] == [1] * 5
        assert len({(row['agent_id'], row['workspace_id'], row['user_id']) for row in scopes}) == 5
        counts = coverage.profile_sampling_diagnostics(runtime, value, tenant_id='audit-tenant')
        assert counts['status'] == 'reported'
        assert [row['natural_decision_count'] for row in counts['scopes']] == [1] * 5
        assert counts['formal_acceptance'] is False and counts['l5_credit'] is False


def test_no_traffic_profile_is_not_forced_g2c_and_explicit_capture_is_not_natural(tmp_path, monkeypatch):
    value = topology(); declared(monkeypatch, value)
    exact = coverage.profile_bindings(value)[-1]['scope']
    with Runtime.create(root=tmp_path) as runtime:
        runtime.store.append(RecordEnvelope.create(kind='evaluation_packet', title='Synthetic explicit metadata', scope=ScopeRef.from_dict(exact),
            source='eimemory.production_recall.explicit_capture',
            content={'request': {'source_ids': ['hermes']}}))
        scopes = agents.recall_agent_scopes(runtime, {'tenant_id': 'audit-tenant'})
        # G2c: zero-traffic declared profiles are not forced through the gate.
        assert scopes == []
        report = agents.run_per_agent(scopes, {}, {}, lambda scope: {
            'sample_count': 0, 'quality_gate': {'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete'}})
        assert not report['combined']['ok']
        assert report['combined']['detail'] == 'no_agent_with_natural_traffic'
        assert report['rule'] == 'g2c_traffic_agents_must_pass_separately'
        assert report['per_agent_force_policy'] == 'g2c_traffic_agents_only.v1'
        assert report['scope_verdicts'] == {}
        assert report['profile_inventory']['logical_profile_count'] == 5
        counts = coverage.profile_sampling_diagnostics(runtime, value, tenant_id='audit-tenant')
        assert counts['scopes'][-1]['explicit_capture_count'] == 1
        assert counts['scopes'][-1]['natural_decision_count'] == 0


def test_unbound_served_profile_is_reported_without_guessed_scope_or_passing_credit(tmp_path, monkeypatch):
    value = topology(unbound=True); declared(monkeypatch, value)
    with Runtime.create(root=tmp_path) as runtime:
        bindings = coverage.profile_bindings(value)
        assert len(bindings) == 4
        for index, binding in enumerate(bindings):
            runtime.store.run_locked(lambda db, b=binding, i=index: decision(db, b['scope'], key='bound-' + str(i)))
        scopes = agents.recall_agent_scopes(runtime, {'tenant_id': 'audit-tenant'})
        assert len(scopes) == 4 and all(row['discovered_decisions'] == 1 for row in scopes)
        result = agents.run_per_agent(scopes, {}, {}, lambda scope: {
            'sample_count': 3, 'quality_gate': {'ok': True}})
        assert result['profile_inventory']['logical_profile_count'] == 5
        assert result['combined']['ok'] is True
        assert result['rule'] == 'g2c_traffic_agents_must_pass_separately'
        assert len(result['agents']) == 4
        assert result['unbound_profiles'] == [{'profile_name': 'audit-d', 'agent_id': 'audit-d',
            'status': 'business_identity_unbound', 'business_qualified': False, 'natural_samples_created': 0}]


def test_capture_status_uses_all_coordinates_and_accepts_five_authorizations(monkeypatch):
    from eimemory.adapters.runtime import host_auth
    value = topology(); declared(monkeypatch, value)
    monkeypatch.setattr(host_auth, 'producer_token_from_private_file', lambda *a, **kw: 'synthetic-producer')
    allowed = coverage.required_capture_scopes(value)
    env = {'EIMEMORY_CAPTURE_QUERY_SCOPES': json.dumps(allowed), 'EIMEMORY_CAPTURE_ORIGINAL_QUERY': '1',
        'EIMEMORY_RECALL_BUDGET_SECONDS': '8', 'EIMEMORY_RUNTIME_COMMIT': COMMIT}
    result = stability._capture_environment_status(env, 'hermes-gateway.service', COMMIT)
    assert result['ok'] and result['capture_scope_count'] == 5
    allowed[2]['user_id'] = 'wrong-user'
    env['EIMEMORY_CAPTURE_QUERY_SCOPES'] = json.dumps(allowed)
    result = stability._capture_environment_status(env, 'hermes-gateway.service', COMMIT)
    assert not result['ok'] and result['profile_capture_coverage']['missing_scope_count'] == 1


def rpc_stub():
    from types import SimpleNamespace
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    calls = []
    bridge = EIBrainRPCBridge(SimpleNamespace())
    def receive(**kwargs):
        calls.append(kwargs)
        return {'ok': True}
    bridge.runtime_adapter = SimpleNamespace(proactive_prefetch=receive, prefetch=receive, proactive_signal=receive)
    return bridge, calls


def test_server_derives_operator_maintenance_even_without_client_envelope(monkeypatch):
    value = operator_topology(); declared(monkeypatch, value)
    bridge, calls = rpc_stub()
    scope = value['profiles'][0]['operator_scope']
    response = bridge.handle({'method': 'adapter.proactive_prefetch', 'params': {
        'channel': 'hermes', 'scope': scope, 'source_ids': ['hermes'],
        'session_id': 'native-local-session', 'turn_id': 'host-turn', 'query': 'Synthetic metadata query',
        'acceptance_generated': False}})
    assert response['ok'] is True and calls[0]['acceptance_generated'] is True
    assert calls[0]['scope'] == scope


def test_server_keeps_business_request_natural_and_forces_operator_explicit_out_of_natural(monkeypatch):
    value = operator_topology(); declared(monkeypatch, value)
    bridge, calls = rpc_stub()
    business = value['profiles'][0]['capture_scopes'][0]['scope']
    response = bridge.handle({'method': 'adapter.proactive_prefetch', 'params': {
        'channel': 'hermes', 'scope': business, 'source_ids': ['hermes'],
        'session_id': 'native-business-session', 'turn_id': 'host-turn', 'query': 'Synthetic query',
        'acceptance_generated': False}})
    assert response['ok'] and calls[-1]['acceptance_generated'] is False
    request = {'session_id': 'native-local-session', 'request_id': 'host-tool-call',
               'acceptance_generated': False, 'capture_mode': 'private'}
    response = bridge.handle({'method': 'adapter.prefetch', 'params': {
        'channel': 'hermes', 'scope': value['profiles'][0]['operator_scope'],
        'query': 'Synthetic local recall', 'task_context': {'source_ids': ['hermes']},
        'explicit_request': request}})
    assert response['ok'] and calls[-1]['explicit_request']['acceptance_generated'] is True
    assert calls[-1]['task_context']['acceptance_generated'] is True
    assert request['acceptance_generated'] is False, 'do not mutate a caller object'


def test_host_signal_binding_is_validated_before_exact_payload_forwarding(monkeypatch):
    value = operator_topology(); declared(monkeypatch, value)
    scope = value['profiles'][0]['operator_scope']
    claim = coverage.request_sampling_binding(channel='hermes', scope=scope, source_ids=['hermes'])
    bridge, calls = rpc_stub()
    response = bridge.handle({'method': 'adapter.proactive_signal', 'params': {
        'channel': 'hermes', 'scope': scope, 'source_ids': ['hermes'], 'runtime_profile_binding': claim,
        'session_id': 'native-session', 'turn_id': 'turn', 'decision_id': 'native-decision',
        'phase': 'used', 'event_id': 'native-event', 'labels': {}}}, attestation_producer='hermes')
    assert response['ok'] and len(calls) == 1
    assert 'runtime_profile_binding' not in calls[0]


@pytest.mark.parametrize('change', ['profile', 'home', 'digest', 'kind', 'eligible', 'wrong_user', 'wrong_source'])
def test_server_rejects_forged_profile_or_scope_instead_of_trusting_caller_flags(monkeypatch, change):
    value = operator_topology(); declared(monkeypatch, value)
    scope = dict(value['profiles'][0]['operator_scope'])
    claim = coverage.request_sampling_binding(channel='hermes', scope=scope, source_ids=['hermes'])
    sources = ['hermes']
    if change == 'wrong_user': scope['user_id'] = 'unregistered-user'
    elif change == 'wrong_source': sources = ['other-source']
    else:
        field = {'profile': 'profile_name', 'home': 'hermes_home', 'digest': 'topology_digest',
                 'kind': 'binding_kind', 'eligible': 'natural_sample_eligible'}[change]
        claim[field] = True if change == 'eligible' else 'forged-value'
    bridge, calls = rpc_stub()
    response = bridge.handle({'method': 'adapter.proactive_prefetch', 'params': {
        'channel': 'hermes', 'scope': scope, 'source_ids': sources, 'runtime_profile_binding': claim,
        'session_id': 'native-session', 'turn_id': 'host-turn', 'query': 'Synthetic query'}})
    assert not response['ok'] and calls == []


def test_semantic_collection_visits_five_declared_scopes_only_and_rejects_missing_authorization(monkeypatch):
    from types import SimpleNamespace
    from eimemory.evaluation import semantic_relevance_monitor as monitor, query_input_vault
    value = topology(); declared(monkeypatch, value)
    allowed = coverage.required_capture_scopes(value)
    selected = []
    def observe(runtime, *, scope, max_new, source_ids):
        selected.append((scope.agent_id, scope.user_id, tuple(sorted(source_ids))))
        return {'status': 'observation_only'}, []
    monkeypatch.setattr(monitor, 'monitor_deliveries', observe)
    monkeypatch.setattr(query_input_vault, 'configured_capture_scopes', lambda: allowed)
    requested = {'tenant_id': 'audit-tenant', 'agent_id': 'default', 'workspace_id': 'office', 'user_id': 'member-0'}
    report, findings = monitor.monitor_channel_deliveries(SimpleNamespace(), scope=requested, include_capture_scopes=True)
    assert len(selected) == 5 and len(set(selected)) == 5
    assert all(row[2] == ('hermes',) for row in selected)
    assert report['profile_inventory']['physical_gateway_count'] == 1 and not findings
    allowed[-1]['workspace_id'] = 'foreign-workspace::channel::hermes'
    with pytest.raises(ValueError, match='capture_scope_not_enabled'):
        monitor.monitor_channel_deliveries(SimpleNamespace(), scope=requested, include_capture_scopes=True)


def test_cli_diagnostics_preserve_profile_counts_without_private_scope_or_false_acceptance(tmp_path, monkeypatch):
    from eimemory.scheduler.result_contract import _per_agent_diagnostics
    from eimemory.cli.main import _nightly_cli_summary
    value = topology(); declared(monkeypatch, value)
    with Runtime.create(root=tmp_path) as runtime:
        scopes = agents.recall_agent_scopes(runtime, {'tenant_id': 'audit-tenant'})
        result = agents.run_per_agent(scopes, {}, {}, lambda scope: {
            'sample_count': 0, 'quality_gate': {'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete'}})
        result['profile_sampling'] = coverage.profile_sampling_diagnostics(runtime, value, tenant_id='audit-tenant')
        # Extra caller fields have no place in bounded diagnostics.
        result['profile_sampling']['scopes'][0]['user_id'] = 'private-raw-user'
        sanitized = _per_agent_diagnostics(result)
        summary = _nightly_cli_summary({'ok': True, 'nightly_diagnostics': {
            'hermes_profile_sampling': sanitized['profile_sampling'],
            'natural_recall_quality_per_agent': sanitized}})
        output = json.dumps(summary)
        assert 'private-raw-user' not in output and 'member-0' not in output
        assert summary['hermes_profile_sampling']['inventory']['logical_profile_count'] == 5
        assert summary['hermes_profile_sampling']['formal_acceptance'] is False
        assert summary['natural_recall_quality_per_agent']['combined']['ok'] is False


@pytest.mark.parametrize('namespace_pids', [[], [100], [100, 1]])
def test_controller_process_observation_accepts_unknown_or_exact_namespace_metadata(monkeypatch, namespace_pids):
    from eimemory.core import runtime_manager
    observation = {'schema': 'eimemory.runtime_process_environment.v1', 'unit': 'hermes-gateway.service',
        'pid': 100, 'start_ticks': 1234, 'invocation_id': 'a' * 32, 'environment': {},
        'namespace_pids': namespace_pids}
    monkeypatch.setattr(runtime_manager, 'request', lambda *a, **kw: {'observation': observation})
    assert stability._host_process_observation({'Id': observation['unit'], 'MainPID': '100'}) == observation


@pytest.mark.parametrize('namespace_pids', [[1, 100], [100, 100], [100, True], [100, 0], '100,1'])
def test_controller_process_observation_rejects_forged_namespace_metadata(monkeypatch, namespace_pids):
    from eimemory.core import runtime_manager
    observation = {'schema': 'eimemory.runtime_process_environment.v1', 'unit': 'hermes-gateway.service',
        'pid': 100, 'start_ticks': 1234, 'invocation_id': 'a' * 32, 'environment': {},
        'namespace_pids': namespace_pids}
    monkeypatch.setattr(runtime_manager, 'request', lambda *a, **kw: {'observation': observation})
    with pytest.raises(ValueError, match='process_observation_invalid'):
        stability._host_process_observation({'Id': observation['unit'], 'MainPID': '100'})


def native_proof(value, process, stamp):
    source_pins = {'source_commit': COMMIT, 'release_root': '/opt/eimemory/releases/' + COMMIT,
        'plugins': [{'name': name, 'target': '/opt/eimemory/releases/' + COMMIT + '/integrations/hermes/' + name,
            'manifest_sha256': 'b' * 64, 'entry_sha256': 'c' * 64} for name in ('eimemory', 'eimemory_hook')]}
    proof = {'schema': native.SCHEMA, 'topology_digest': value['topology_digest'],
        'release_source_pins': source_pins,
        'owner': {'unit': 'hermes-gateway.service', 'pid': process['pid'], 'start_ticks': process['start_ticks'],
                  'invocation_id': process['invocation_id']},
        'served_profiles': sorted(row['profile_name'] for row in value['profiles']),
        'profile_configs': [{**{key: row[key] for key in ('profile_name', 'hermes_home')},
            'ok': True, 'reason': '', 'provider': 'eimemory', 'hook_enabled': True, 'config_sha256': 'a' * 64,
            'plugin_links': [{'name': name, 'target': '/opt/eimemory/releases/' + COMMIT + '/integrations/hermes/' + name,
                'manifest_sha256': 'b' * 64, 'entry_sha256': 'c' * 64} for name in ('eimemory', 'eimemory_hook')]}
            for row in sorted(value['profiles'], key=lambda item: item['profile_name'])],
        'complete': True, 'blocking_reasons': [], 'observed_at': stamp, 'gateway_updated_at': stamp}
    proof['stable_digest'] = sha256(canonical_bytes(native._stable_body(proof))).hexdigest()
    return proof


def topology_samples(value):
    result = samples()
    required = coverage.required_capture_scopes(value)
    for sample in result:
        sample.update(schema=stability.TOPOLOGY_SAMPLE_SCHEMA, topology=value)
        sample['capture']['processes'] = sample['capture']['processes'][:2]
        sample['units'] = [row for row in sample['units'] if not row['unit'].startswith('hermes-gateway-')]
        for process in sample['capture']['processes']:
            process.update(invocation_id='a' * 32, capture_scope_count=len(required),
                profile_capture_coverage=coverage.capture_coverage(required, value))
        sample['native_profile_topology'] = native_proof(value, sample['capture']['processes'][1], sample['observed_at'])
        seal(sample)
    return result


def test_two_owner_v2_native_roster_is_accepted_but_v1_cannot_shrink_inventory():
    value = topology(); observations = topology_samples(value)
    report = stability.assess_controlled_stability(observations, expected_commit=COMMIT, expected_topology=value)
    assert report['ok'] and report['schema'] == stability.TOPOLOGY_SCHEMA
    assert report['runtime_units'] == value['runtime_units']
    assert report['natural_certified'] is False
    with pytest.raises(ValueError, match='runtime_inventory_invalid'):
        stability.assess_controlled_stability(samples(), expected_commit=COMMIT,
            expected_runtime_units=value['runtime_units'])


@pytest.mark.parametrize('failure', ['gateway_crashed', 'profile_missing', 'owner_changed', 'config_changed', 'wrong_capture_scope'])
def test_single_gateway_failures_cannot_shrink_expected_roster(failure):
    value = topology(); observations = topology_samples(value)
    last = observations[-1]
    if failure == 'gateway_crashed':
        last['units'] = [row for row in last['units'] if row['unit'] != 'hermes-gateway.service']
    elif failure == 'profile_missing':
        last['native_profile_topology']['served_profiles'].pop()
    elif failure == 'owner_changed':
        last['native_profile_topology']['owner']['pid'] += 1
    elif failure == 'config_changed':
        last['native_profile_topology']['profile_configs'][0]['config_sha256'] = 'd' * 64
    else:
        last['capture']['processes'][1]['profile_capture_coverage']['missing_scope_count'] = 1
    if failure in {'profile_missing', 'owner_changed', 'config_changed'}:
        last['native_profile_topology']['stable_digest'] = sha256(canonical_bytes(native._stable_body(last['native_profile_topology']))).hexdigest()
    seal(last)
    assert not stability.assess_controlled_stability(observations, expected_commit=COMMIT, expected_topology=value)['ok']


def test_cpa_v2_revalidates_same_agent_two_users_without_overwriting_either_scope(setup):
    from dataclasses import asdict, replace
    from eimemory.evaluation import controlled_production_acceptance as cpa
    extra = replace(SCOPES[0], user_id='second-business-user')
    setup.policy['schema'] = cpa.EXACT_SCOPE_POLICY_SCHEMA
    setup.policy['agents'].append({'scope': asdict(extra), 'source_ids': ['hermes']})
    records = []
    for index in range(3):
        original = setup.memories[SCOPES[0].agent_id][index]
        record = RecordEnvelope.create(kind='memory', title=original.title, summary=original.summary,
            content=deepcopy(original.content), source=original.source, source_id='hermes',
            scope=extra, meta=deepcopy(original.meta))
        record.time.created_at = record.time.updated_at = '2020-01-01T00:00:00Z'
        setup.runtime.store.append(record)
        records.append(record)
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        if ScopeRef.from_dict(kwargs['scope']) == extra:
            bundle.items = [next(record for record in records if record.title == kwargs['query'])]
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    report = cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(OWNER))
    assert report['accepted'] and report['schema'] == cpa.EXACT_SCOPE_SCHEMA, report
    repeated = [row for row in report['agents'] if row['agent_id'] == extra.agent_id]
    assert len(repeated) == 2 and len({row['scope_digest'] for row in repeated}) == 2
    verified = cpa.verify_controlled_production_acceptance(setup.runtime, scope=asdict(OWNER), record_id=report['record_id'])
    assert verified['accepted'] and verified['agents'] == report['agents']
    # The v1 contract and old signed evidence keep their original uniqueness.
    legacy = {**setup.policy, 'schema': cpa.POLICY_SCHEMA}
    with pytest.raises(ValueError, match='policy_agents_invalid'):
        cpa._validate_policy(legacy, OWNER)
    overlap = deepcopy(setup.policy)
    overlap['agents'].append(deepcopy(overlap['agents'][-1]))
    with pytest.raises(ValueError, match='policy_agents_invalid'):
        cpa._validate_policy(overlap, OWNER)


def test_v2_scope_source_transport_matching_and_agent_summary_never_drop_a_scope():
    from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
    from eimemory.scheduler.controlled_acceptance_diagnostics import _historical_business_agents
    from eimemory.scheduler.result_contract import _historical_business_per_agent_diagnostics
    from test_controlled_production_release_closure import complete_report
    report = complete_report()
    controlled = report['controlled_production_acceptance']
    controlled['schema'] = 'controlled_production_acceptance.v2'
    controlled['agents'][1]['agent_id'] = controlled['agents'][0]['agent_id']
    report['rpc_controlled_probe']['schema'] = 'controlled_production_rpc_probe.v2'
    report['rpc_controlled_probe']['cases'][1]['agent_id'] = controlled['agents'][0]['agent_id']
    assert controlled_production_closure_report_ok(report)
    summaries = _historical_business_agents(controlled['agents'], exact_scopes=True)
    assert summaries == {'primary': {'sample_count': 6}}
    assert _historical_business_agents(controlled['agents']) == {}, 'v1 retains original uniqueness'
    aggregate = _historical_business_per_agent_diagnostics({'agents': controlled['agents'],
        'sample_count': 6, 'pass_count': 6, 'fail_count': 0})
    assert aggregate['agents']['primary']['sample_count'] == 6 and aggregate['combined']['agent_count'] == 1
    forged = deepcopy(report)
    forged['rpc_controlled_probe']['cases'][1]['scope_digest'] = controlled['agents'][0]['scope_digest']
    assert not controlled_production_closure_report_ok(forged)
    distinct_source = deepcopy(forged)
    distinct_source['controlled_production_acceptance']['agents'][1]['scope_digest'] = controlled['agents'][0]['scope_digest']
    distinct_source['controlled_production_acceptance']['agents'][1]['source_ids'] = ['other-source']
    distinct_source['rpc_controlled_probe']['cases'][1]['source_ids'] = ['other-source']
    assert controlled_production_closure_report_ok(distinct_source)
    distinct_source['controlled_production_acceptance']['agents'][1]['source_ids'] = ['hermes', 'other-source']
    distinct_source['rpc_controlled_probe']['cases'][1]['source_ids'] = ['hermes', 'other-source']
    assert not controlled_production_closure_report_ok(distinct_source)
    assert not _historical_business_agents(distinct_source['controlled_production_acceptance']['agents'], exact_scopes=True)
