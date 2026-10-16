"""Independent scheduler/binding/storage regression with synthetic fixtures."""
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_SOURCE, PENDING_SOURCE, build_production_query_dataset,
)
from eimemory.governance.learning.supervisor import build_supervisor_contract
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler import jobs
from test_logical_scope_pin_alias import (
    HOST, SCOPE, bindings, legacy_pin, isolate_identity,
)


@pytest.mark.parametrize('case', ['valid', 'missing_file', 'missing_user', 'ambiguous_user',
                                  'ambiguous_channel', 'other_tenant', 'other_workspace',
                                  'unconfigured_agent', 'explicit_other_user'])
def test_scheduler_binding_dataset_and_durable_summary(tmp_path, monkeypatch, case):
    pins = [legacy_pin()]
    requested = dict(HOST)
    if case == 'missing_user':
        del pins[0]['user_id']
    elif case == 'ambiguous_user':
        pins.append(legacy_pin(user_id='different-member'))
    elif case == 'ambiguous_channel':
        pins.append(resolve_channel_scope('codex', SCOPE))
    elif case == 'other_tenant':
        pins[0]['tenant_id'] = 'different-tenant'
    elif case == 'other_workspace':
        pins[0]['workspace_id'] = 'different-workspace::channel::hermes'
    elif case == 'unconfigured_agent':
        pins[0]['agent_id'] = 'unconfigured-agent'
    elif case == 'explicit_other_user':
        requested['user_id'] = 'explicit-other-member'
    if case != 'missing_file':
        bindings(tmp_path, monkeypatch, pins)

    # Keep actual scheduler, collection, controlled generation, evaluation and
    # persistence. Other nightly jobs are outside this scope investigation.
    original_step = jobs._nightly_step
    exercised = {'memory_eval_ci', 'production_recall_collection', 'production_recall'}
    def bounded_step(reports, name, run):
        return original_step(reports, name, run if name in exercised else lambda: {'ok': True})
    monkeypatch.setattr(jobs, '_nightly_step', bounded_step)
    root = tmp_path / 'runtime'
    exact = resolve_channel_scope('hermes', SCOPE)
    with Runtime.create(root=root) as runtime:
        source = RecordEnvelope.create(
            kind='memory', title='Atlas notebooks', summary='Atlas notebooks are green',
            content={'text': 'Atlas notebooks are green'}, source='operator.memory', source_id='hermes',
            scope=ScopeRef.from_dict(exact), meta={'memory_type': 'fact', 'force_capture': True})
        source.time.created_at = '2026-10-10T00:00:00Z'
        runtime.store.append(source)
        runtime.store.record_proactive_decision({
            'decision_id': 'natural-decision', 'channel': 'hermes', 'scope': exact,
            'source_key': sha256(b'hermes').hexdigest(), 'source_ids': ['hermes'],
            'session_id': 'session', 'turn_id': 'turn', 'query_id': 'query',
            'query_digest': sha256(b'notebook colour').hexdigest(),
            'effective_query_digest': sha256(b'notebook colour').hexdigest(),
            'task_type': 'memory.recall', 'policy_version': 'proactive.test.v1',
            'release_identity': {'release_commit': 'a' * 40, 'release_version': '1.14.112',
                                 'deployment_receipt_id': 'receipt', 'release_session_id': 'session'},
            'release_bound': True, 'control_cohort': False, 'pair_id': 'pair',
        }, [], [])
        monkeypatch.setattr(runtime, 'build_replay_dataset', lambda **kwargs: {'ok': True, 'cases': []})
        report = jobs.run_nightly_jobs(runtime, scope=requested)
        assert 'supervisor_summary' in report
        assert 'nightly_unhandled_exception' not in report['nightly_diagnostics']['failed_steps']
        memory = report['memory_eval_ci']
        collection = report['production_recall_collection']
        resolves_user = case in {'valid', 'ambiguous_channel'}
        assert collection['new_count'] == (1 if resolves_user else 0)
        assert collection['decision_count'] == (1 if resolves_user else 0)
        assert build_production_query_dataset(runtime, scope=requested)['progress']['accepted_case_count'] == 0
        assert report['nightly_diagnostics']['recall_quality_accepted'] is False
        assert report['nightly_diagnostics']['memory_benchmark_accepted'] is False
        if case == 'valid':
            assert memory['dataset_source'] == 'controlled_records'
            assert memory['retrieval_case_count'] == 1
            assert memory['controlled_context']['resolution']['matching_channel_count'] == 1
            assert memory['controlled_context']['resolution']['logical_user_rewritten'] is True
            assert memory['persisted'] is True
            assert report['production_recall'].get('blocked_reason') != 'dataset_scope_override_mismatch'
        else:
            expected = ('memory_benchmark_bindings_missing' if case == 'missing_file' else
                        'memory_benchmark_channel_binding_ambiguous' if case == 'ambiguous_channel' else
                        'memory_benchmark_channel_binding_missing')
            assert memory['controlled_context']['reason'] == expected
            assert memory['retrieval_case_count'] == 0
        supervisor_id = report['supervisor_summary']['record_id']
        diagnostics = report['nightly_diagnostics']
        pending_ids = collection['new_pending_record_ids']
        assert not [record for record in runtime.store.list_records(kinds=['evaluation_packet'], scope=exact)
                    if record.source == ACCEPTED_SOURCE]
    with Runtime.create(root=root) as runtime:
        saved = runtime.store.get_by_id(supervisor_id, scope=ScopeRef.from_dict(requested))
        assert saved.content['nightly_diagnostics'] == diagnostics
        assert build_supervisor_contract(runtime, scope=requested)['runs']['nightly']['nightly_diagnostics'] == diagnostics
        for record_id in pending_ids:
            pending = runtime.store.get_by_id(record_id)
            assert asdict(pending.scope) == exact
            assert pending.source == PENDING_SOURCE
            assert pending.status == 'active'
        assert build_production_query_dataset(runtime, scope=requested)['progress']['accepted_case_count'] == 0
        second_collection = jobs._run_production_recall_collection(runtime, scope=requested)
        assert second_collection['new_count'] == 0
        assert second_collection['existing_count'] == (1 if resolves_user else 0)
        if case == 'valid':
            saved_memory = runtime.store.get_by_id(memory['persisted_record_id'])
            assert saved_memory.content['report']['retrieval_case_count'] == 1
            assert saved_memory.content['report']['memory_benchmark_accepted'] is False
    serialized = json.dumps(diagnostics)
    assert 'notebooks are green' not in serialized
    assert str(tmp_path) not in serialized
    assert HOST['user_id'] not in serialized
