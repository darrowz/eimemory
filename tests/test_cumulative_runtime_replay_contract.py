"""Local fake regressions: no Runtime, provider, credentials, or network."""
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.governance.learning import replay_dataset as dataset
from eimemory.governance.l5 import real_replay_gate as gate
from eimemory.scheduler import jobs


class TransactionFake:
    def __init__(self):
        self.in_transaction = True
        self.calls = []
        self.pending = ['caller-owned']

    def execute(self, sql, *args):
        self.calls.append(sql)
        if sql == 'BEGIN IMMEDIATE':
            raise RuntimeError('nested transaction')
        return SimpleNamespace(rowcount=0)

    def rollback(self):
        self.calls.append('rollback')
        self.in_transaction = False
        self.pending.clear()

    def commit(self):
        self.calls.append('commit')
        self.in_transaction = False
        self.pending.clear()


@pytest.mark.parametrize('operation', [
    'append_proactive_turn', 'transition_proactive_decision',
    'record_proactive_outcome', 'append_proactive_bypass', 'rewrite',
    'repair_status_projection_mismatches', 'record_event', 'record_outcome',
    'record_terminal_bundle', 'upsert_intent_pattern', 'rollback_intent_pattern',
    'upsert_memory_edge', 'upsert_memory_edges', 'update_intent_pattern_row',
])
def test_commit_owner_leaves_outer_transaction_untouched(operation):
    store = object.__new__(RuntimeStore)
    store._lock = RLock()
    store.sqlite = TransactionFake()
    scope = ScopeRef(tenant_id='fake')
    record = RecordEnvelope.create(kind='memory', title='fake', scope=scope)
    calls = {
        'append_proactive_turn': lambda: store.append_proactive_turn({}),
        'transition_proactive_decision': lambda: store.transition_proactive_decision('d', {}, {}),
        'record_proactive_outcome': lambda: store.record_proactive_outcome('d', {}),
        'append_proactive_bypass': lambda: store.append_proactive_bypass({}),
        'rewrite': lambda: store.rewrite(record),
        'repair_status_projection_mismatches': lambda: store.repair_status_projection_mismatches(scope=scope),
        'record_event': lambda: store.record_event({}, scope=scope),
        'record_outcome': lambda: store.record_outcome('e', {}, scope=scope),
        'record_terminal_bundle': lambda: store.record_terminal_bundle(
            verified_receipts=[], channel='fake', session_id='s', run_id='r',
            trace_id='t', event_payload={}, outcome_payload={}, trace_record=record, scope=scope),
        'upsert_intent_pattern': lambda: store.upsert_intent_pattern({}, scope=scope),
        'rollback_intent_pattern': lambda: store.rollback_intent_pattern('p', scope=scope),
        'upsert_memory_edge': lambda: store.upsert_memory_edge(None),
        'upsert_memory_edges': lambda: store.upsert_memory_edges([]),
        'update_intent_pattern_row': lambda: store.update_intent_pattern_row(
            pattern_id='p', scope_ref=scope, status='active', payload_json='{}',
            last_rollback_reason='', updated_at='fake'),
    }
    with pytest.raises(RuntimeError, match=f'^{operation}_requires_own_transaction$'):
        calls[operation]()
    assert store.sqlite.calls == []
    assert store.sqlite.in_transaction and store.sqlite.pending == ['caller-owned']


def test_commit_false_pattern_update_can_borrow():
    store = object.__new__(RuntimeStore)
    store._lock = RLock()
    store.sqlite = TransactionFake()
    assert store.update_intent_pattern_row(
        pattern_id='p', scope_ref=ScopeRef(tenant_id='fake'), status='active',
        payload_json='{}', last_rollback_reason='', updated_at='fake', commit=False) == 0
    assert store.sqlite.in_transaction and store.sqlite.pending == ['caller-owned']
    assert len(store.sqlite.calls) == 1


@pytest.mark.parametrize('failure', [False, True])
def test_capture_callback_borrows_savepoint_without_outer_commit(failure):
    store = object.__new__(RuntimeStore)
    store._lock = RLock()
    store.sqlite = TransactionFake()
    store.sqlite.insert_proactive_decision = Mock(return_value=({'id': 'fake'}, False))
    callback = Mock(side_effect=ValueError('fake capture failure') if failure else None)
    if failure:
        with pytest.raises(ValueError, match='fake capture failure'):
            store.record_proactive_decision({}, [], [], capture_input=callback)
        assert store.sqlite.calls == ['SAVEPOINT proactive_decision',
                                      'ROLLBACK TO proactive_decision', 'RELEASE proactive_decision']
    else:
        assert store.record_proactive_decision({}, [], [], capture_input=callback) == ({'id': 'fake'}, False)
        assert store.sqlite.calls == ['SAVEPOINT proactive_decision', 'RELEASE proactive_decision']
    callback.assert_called_once_with()
    assert store.sqlite.in_transaction and store.sqlite.pending == ['caller-owned']


def catalog_case(**updates):
    return dict(case_id='Case-A', source='capability_evaluation_catalog',
                target_capability='capability.fake', capability_revision_id='Rev-A',
                provider_binding_id='Bind-A', eval_spec_id='Spec-A', evaluation_case_digest='Digest-A',
                query='Verify the release evidence for this task', expected='Preserve exact source identity',
                expected_text=['Check identity', 'Preserve evidence', 'Reject conflicts'],
                execution_type='capability_evaluation', executor_id='fake', evaluation_input={'value': 1},
                **updates)


def test_catalog_case_identity_is_exact_and_binding_complete():
    case = catalog_case()
    other = {**case, 'provider_binding_id': 'Bind-B'}
    assert len(dataset._dedupe_cases([case, other])) == 2
    assert len(dataset._dedupe_cases([case, {**case, 'case_id': 'case-a'}])) == 2


def test_conflicting_catalog_identity_rejects_both():
    case = catalog_case()
    assert dataset._dedupe_cases([case, {**case, 'evaluation_input': {'value': 2}}]) == []


def test_fingerprint_includes_payload_and_execution_route():
    case = catalog_case()
    assert dataset._case_fingerprint([case]) != dataset._case_fingerprint([{**case, 'executor_id': 'other'}])


def test_final_set_metrics_and_route_survive():
    case = catalog_case()
    quality, final = dataset._prepare_replay_dataset_cases([case, case], limit=1)
    assert final['case_quality_breakdown']['accepted'] == 1
    assert final['selection_breakdown']['duplicate_dropped'] == 1
    for key in ('execution_type', 'executor_id', 'evaluation_input'):
        assert final['cases'][0][key] == case[key]


def test_final_catalog_dataset_dispatches_only_retrieval_to_memory_eval(monkeypatch):
    from eimemory.evaluation import capability_catalog
    from eimemory.governance.capability import capability_replay_executor
    cases = [catalog_case(), {**catalog_case(), 'case_id': 'Case-B', 'execution_type': 'retrieval'},
             {**catalog_case(), 'case_id': 'Case-C', 'source': 'outcome_trace',
              'source_record_id': 'ref_actual', 'execution_type': 'recorded_execution'}]
    _, final = dataset._prepare_replay_dataset_cases(cases, limit=10)
    executor = SimpleNamespace(execute=Mock(return_value={'verdict': 'pass'}))
    runtime = SimpleNamespace(capability_catalog=executor, store=SimpleNamespace(append=Mock()),
                              run_memory_eval_ci=Mock(return_value={'ok': True}))
    monkeypatch.setattr(jobs, '_memory_eval_ci_dataset', lambda *args, **kwargs: (final, True, 'fake'))
    monkeypatch.setattr(capability_catalog, 'resolve_application_capability_catalog', lambda catalog: catalog)
    replay = Mock(return_value={'verdict': 'pass'})
    monkeypatch.setattr(capability_replay_executor, 'execute_capability_replay_case', replay)
    result = jobs._run_memory_eval_ci(runtime, scope={})
    assert result['ok'] and result['execution_counts']['pass'] == 2
    submitted = runtime.run_memory_eval_ci.call_args.args[0]['cases']
    assert [case['case_id'] for case in submitted] == ['Case-B']
    dispatched = executor.execute.call_args.args[0]
    assert dispatched['input'] == {'value': 1} and dispatched['executor_id'] == 'fake'
    replay.assert_called_once()


@pytest.mark.parametrize('report', [
    {'effects_unknown': True}, {'timeout_exceeded': True},
    {'blocked_reason': 'scheduler_timeout_lease_not_reread'},
    {'learning_skipped_reason': 'scheduler_lease_effects_unknown_after_timeout'},
])
def test_l5_does_not_restart_uncertain_upstream(monkeypatch, report):
    monkeypatch.setenv('EIMEMORY_L5_LOOP_ENABLED', '1')
    runtime = SimpleNamespace(run_l5_cycle=Mock(side_effect=AssertionError('must not execute')))
    result = jobs._run_l5_loop(runtime, scope={}, autonomous_learning_report=report)
    assert result.get('execution_attempted') is False
    assert result['status'] == 'blocked'
    runtime.run_l5_cycle.assert_not_called()


def test_real_replay_uses_caller_catalog_without_fixed_type_quota(monkeypatch):
    scope = ScopeRef(tenant_id='fake')
    catalog = object()
    samples = [dict(source_record_id=f'source-{i}', source_evidence_digest=f'digest-{i}',
                    source_task_type='only-enabled-type', terminal_evidence_digest=f'terminal-{i}',
                    executed=True, passed=True) for i in range(10)]
    report = dict(real_provenance_contract=gate.REAL_PROVENANCE_CONTRACT,
                  package_tree_digest='fake-digest', samples=samples)
    runtime = SimpleNamespace(store=SimpleNamespace(latest_record_by_meta_value_exact_scope=
                              Mock(return_value=SimpleNamespace(content={'report': report}, record_id='fake'))))
    monkeypatch.setattr(gate, 'runtime_package_tree_digest', lambda: 'fake-digest')
    def validate(runtime, *, source_record_id, scope, catalog):
        assert catalog is expected_catalog
        i = source_record_id.split('-')[-1]
        return dict(ok=True, source_evidence_digest=f'digest-{i}',
                    task_type='only-enabled-type', terminal_evidence_digest=f'terminal-{i}')
    expected_catalog = catalog
    monkeypatch.setattr(gate, 'validate_real_replay_source', validate)
    result = gate.build_verified_real_replay_summary(runtime, scope=scope, catalog=catalog)
    assert result['ok'] and result['distinct_task_types'] == 1
