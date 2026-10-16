"""A channel dataset needs the exact private pin, never metadata authority."""
from dataclasses import asdict
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate
from eimemory.governance.learning.supervisor import build_supervisor_contract
from eimemory.scheduler import jobs
from test_logical_scope_pin_alias import HOST, SCOPE, bindings, legacy_pin, isolate_identity
from test_nightly_production_recall_scope import bound_dataset
from test_production_real_query_gate import RELEASE


@pytest.mark.parametrize('binding_case', ['valid', 'explicit_logical_user', 'missing', 'ambiguous_channel',
                                         'other_workspace', 'other_user', 'other_tenant',
                                         'unconfigured_agent', 'explicit_other_user', 'explicit_other_channel'])
def test_exact_bound_channel_dataset_through_scheduler_and_persist(
        tmp_path, monkeypatch, trusted_dataset_path_ancestors, binding_case):
    pins = [legacy_pin()]
    requested = dict(HOST)
    if binding_case == 'explicit_logical_user':
        requested = dict(SCOPE)
    elif binding_case == 'explicit_other_user':
        requested['user_id'] = 'explicit-member'
    elif binding_case == 'explicit_other_channel':
        requested = resolve_channel_scope('codex', requested)
    if binding_case == 'ambiguous_channel':
        pins.append(resolve_channel_scope('codex', SCOPE))
    elif binding_case == 'other_workspace':
        pins[0]['workspace_id'] = 'other-workspace::channel::hermes'
    elif binding_case == 'other_user':
        pins[0]['user_id'] = 'other-member'
    elif binding_case == 'other_tenant':
        pins[0]['tenant_id'] = 'other-tenant'
    elif binding_case == 'unconfigured_agent':
        pins[0]['agent_id'] = 'unconfigured-agent'
    if binding_case != 'missing':
        bindings(tmp_path, monkeypatch, pins)
    exact = resolve_channel_scope('hermes', SCOPE)
    dataset = bound_dataset(exact)
    dataset['cases'] = [case for case in dataset['cases'] if case['channel'] == 'hermes']
    dataset.pop('_secure_dataset_evidence')
    path = tmp_path / 'channel-dataset.json'
    path.write_text(json.dumps(dataset))
    path.chmod(0o600)
    monkeypatch.setenv('EIMEMORY_PRODUCTION_RECALL_DATASETS', json.dumps({SCOPE['agent_id']: str(path)}))
    monkeypatch.setattr(real_query_gate, 'current_release_identity', lambda *args: RELEASE)
    hydration_scopes = []
    def missing_labels(runtime, cases, **kwargs):
        hydration_scopes.extend(case['scope'] for case in cases)
        return False, 'label_authority_required', {}
    monkeypatch.setattr(real_query_gate, '_hydrate_real_query_labels', missing_labels)
    original_step = jobs._nightly_step
    monkeypatch.setattr(jobs, '_nightly_step', lambda reports, name, run:
                        original_step(reports, name, run if name == 'production_recall' else lambda: {'ok': True}))
    root = tmp_path / 'runtime'
    with Runtime.create(root=root) as runtime:
        report = jobs.run_nightly_jobs(runtime, scope=requested)
        assert 'nightly_unhandled_exception' not in report['nightly_diagnostics']['failed_steps']
        gate = report['production_recall']
        assert 'accepted' in gate, gate
        assert gate['accepted'] is False
        assert report['nightly_diagnostics']['recall_quality_accepted'] is False
        if binding_case in {'valid', 'explicit_logical_user'}:
            assert gate['blocked_reason'] == 'label_authority_required'
            assert hydration_scopes == [exact] * 5
        else:
            assert gate['blocked_reason'] == 'dataset_scope_override_mismatch'
            assert not hydration_scopes
        assert gate['persisted'] is True
        gate_id = gate['persisted_record_id']
        supervisor_id = report['supervisor_summary']['record_id']
        assert asdict(runtime.store.get_by_id(gate_id).scope) == (exact if binding_case in {'valid', 'explicit_logical_user'} else
               {**HOST, 'user_id': SCOPE['user_id']} if binding_case == 'ambiguous_channel' else
               {**HOST, 'user_id': 'other-member'} if binding_case == 'other_user' else
               resolve_channel_scope('codex', SCOPE) if binding_case == 'explicit_other_channel' else requested)
    with Runtime.create(root=root) as runtime:
        saved = runtime.store.get_by_id(gate_id).content['report']
        assert saved['blocked_reason'] == gate['blocked_reason']
        assert saved['accepted'] is False
        assert runtime.store.get_by_id(supervisor_id).content['nightly_diagnostics'] == report['nightly_diagnostics']
        assert build_supervisor_contract(runtime, scope=requested)['runs']['nightly']['nightly_diagnostics'] == report['nightly_diagnostics']
