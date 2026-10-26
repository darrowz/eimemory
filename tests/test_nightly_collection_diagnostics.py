"""New recall cohorts keep bounded native links through durable nightly receipts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_auto_review import auto_review_pending_production_queries
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.scheduler import jobs
from eimemory.scheduler.collection_diagnostics import production_collection_diagnostics
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_production_query_auto_review import BASE_SCOPE, _keys, _seed
from test_production_recall_collection import _flood


@pytest.fixture(autouse=True)
def _collection_environment(monkeypatch):
    monkeypatch.delenv(jobs.PRODUCTION_RECALL_AUTO_COLLECT_FLAG, raising=False)
    monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', raising=False)


def _pending(index):
    return 'prqp_' + f'{index:032x}'


def _case(index, candidate_count=1):
    return {'pending_record_id': _pending(index),
            'capture_ref': 'pd:' + f'{index:032x}', 'candidate_count': candidate_count}


def _review(index, **overrides):
    return {'pending_record_id': _pending(index), 'review_receipt_id': 'pret_' + f'{index:032x}',
            'review_verdict': 'fail', 'review_complete': True,
            'reasons': ['independent_signal_agreement_missing'], **overrides}


def _collection(*cases, **overrides):
    return {'ok': True, 'status': 'completed', 'policy': {'enabled': True},
            'new_count': len(cases), 'existing_count': 0,
            'new_pending_record_ids': [case['pending_record_id'] for case in cases],
            'new_case_refs': list(cases), 'new_pending_record_ids_truncated': False,
            'skipped': {}, **overrides}


def test_only_this_runs_new_cohort_gets_exact_review_associations():
    collection = _collection(_case(1), _case(2, 0))
    reviews = {'review_results': [_review(999, reasons=['semantic_off_topic']),
                                 _review(2, reasons=['no_candidate_refs']), _review(1)]}
    snapshot = deepcopy((collection, reviews))
    result = production_collection_diagnostics(collection, reviews)
    assert result['new_count'] == 2
    assert [case['pending_record_id'] for case in result['new_case_reviews']] == [_pending(1), _pending(2)]
    assert result['new_case_reviews'][0]['review_receipt_id'] == _review(1)['review_receipt_id']
    assert result['new_case_reviews'][1]['candidate_count'] == 0
    assert result['new_case_reviews'][1]['reasons'] == ['no_candidate_refs']
    assert _pending(999) not in json.dumps(result)
    assert 'semantic_off_topic' not in json.dumps(result)
    assert (collection, reviews) == snapshot


def test_missing_collection_is_unknown_and_semantic_new_count_cannot_replace_it():
    diagnostics = nightly_result_diagnostics({'semantic_relevance_monitor': {'new_count': 9}}, [])
    result = diagnostics['production_recall_collection']
    assert diagnostics['recall_semantic_relevance']['new_count'] == 9
    assert result['status'] == 'not_reported'
    for key in ('new_count', 'existing_count', 'decision_count', 'empty_result_count',
                'new_nonempty_count', 'new_empty_count', 'skipped_reason_counts',
                'new_pending_record_ids', 'new_pending_record_ids_truncated'):
        assert result[key] is None
    assert result['new_case_reviews'] == []


def test_missing_same_run_review_is_not_reconstructed_from_other_case():
    result = production_collection_diagnostics(_collection(_case(1)), {
        'review_results': [_review(2)]})
    case = result['new_case_reviews'][0]
    assert case['review_status'] == 'not_reported'
    assert case['review_receipt_id'] is None
    assert case['reasons'] is None
    assert case['signal_counts']['delivered'] is None
    missing = production_collection_diagnostics(_collection(_case(1)), {})
    assert missing['review_results_status'] == 'not_reported'
    assert missing['review_results_truncated'] is None


def test_native_store_unavailable_does_not_become_zero_or_empty_cohort():
    report = jobs._run_production_recall_collection(SimpleNamespace(store=object()), scope=BASE_SCOPE)
    assert report['ok'] is False and report['status'] == 'blocked'
    assert report['blocked_reason'] == 'proactive_audit_store_unavailable'
    result = production_collection_diagnostics(report, {})
    for key in ('new_count', 'existing_count', 'decision_count', 'empty_result_count',
                'new_nonempty_count', 'new_empty_count', 'skipped_reason_counts',
                'new_pending_record_ids', 'new_pending_record_ids_truncated'):
        assert result[key] is None


def test_disabled_collection_does_not_touch_store_or_report_no_new_cases(monkeypatch):
    monkeypatch.setenv(jobs.PRODUCTION_RECALL_AUTO_COLLECT_FLAG, '0')
    report = jobs._run_production_recall_collection(SimpleNamespace(), scope=BASE_SCOPE)
    result = production_collection_diagnostics(report, {})
    assert result['status'] == 'disabled'
    assert result['enabled'] is False
    assert result['new_count'] is None and result['new_pending_record_ids'] is None


def test_private_text_scope_identities_and_invalid_native_refs_never_enter_summary():
    collection = _collection(_case(1), _case(2), skipped={
        'non_exact_source': 2, 'private-secret-reason': 4}, scope_resolution={
        'requested_user_id': 'private-secret-host', 'user_id': 'private-secret-member',
        'reason': 'host_user_replaced_by_bound_logical_user', 'rewritten': True})
    collection['new_pending_record_ids'].append('private-secret-id')
    collection['new_case_refs'][0]['capture_ref'] = 'private-secret-capture'
    reviews = {'review_results': [_review(1, review_receipt_id='private-secret-receipt',
        reasons=['semantic_off_topic', 'private-secret-reason'],
        semantic_quality={'status': 'evaluated', 'verdict': 'off_topic',
                          'query': 'private-secret-query'},
        signal_counts={'delivered': 1, 'host_used': False, 'private-secret-signal': 1},
        detail='private-secret-body')], 'scope_resolution': {'user_id': 'private-secret-other'}}
    result = production_collection_diagnostics(collection, reviews)
    assert result['invalid_new_pending_id_count'] == 1
    assert result['new_case_reviews'][0]['capture_ref'] is None
    assert result['new_case_reviews'][0]['review_receipt_id'] is None
    assert result['new_case_reviews'][0]['semantic_quality'] == {'status': 'evaluated', 'verdict': 'off_topic'}
    assert result['new_case_reviews'][0]['signal_counts']['host_used'] is None
    assert result['skipped_reason_counts'] == {'non_exact_source': 2, 'reason_not_allowlisted': 4}
    assert result['scope_resolution'] == {'rewritten': True, 'reason': 'host_user_replaced_by_bound_logical_user'}
    assert 'private-secret' not in json.dumps(result)


def test_upstream_truncation_survives_second_projection_and_review_scan_window():
    result = production_collection_diagnostics(_collection(_case(1), new_count=120,
        new_pending_record_ids_truncated=True), {'review_results': [_review(1)]})
    assert result['new_count'] == 120
    assert result['new_pending_record_ids'] == [_pending(1)]
    assert result['new_pending_record_ids_truncated'] is True
    assert len(result['new_case_reviews']) == 1
    raw = _collection(*[_case(index) for index in range(51)])
    bounded = production_collection_diagnostics(raw, {'review_results': []})
    assert len(bounded['new_case_reviews']) == 50
    assert bounded['new_pending_record_ids_truncated'] is True
    beyond_review_limit = production_collection_diagnostics(_collection(_case(1)), {
        'review_results': [_review(2)] * 1500 + [_review(1)]})
    assert beyond_review_limit['review_results_truncated'] is True
    assert beyond_review_limit['new_case_reviews'][0]['review_status'] == 'not_reported'


def test_upstream_review_truncation_is_not_erased_by_a_short_retained_list():
    result = production_collection_diagnostics(_collection(_case(1)), {
        'review_results': [_review(1)], 'review_results_truncated': True})
    assert result['review_results_status'] == 'reported'
    assert result['review_results_truncated'] is True
    assert result['new_case_reviews'][0]['review_receipt_id'] == _review(1)['review_receipt_id']


@pytest.mark.parametrize('early_return', ['capture_missing', 'semantic_off_topic'])
def test_native_early_review_returns_do_not_certify_unchecked_initialized_signal_counts(tmp_path, early_return):
    with Runtime.create(root=tmp_path) as runtime:
        _, decision_id = _seed(runtime, 1700,
                               semantic='unrelated' if early_return == 'semantic_off_topic' else 'relevant')
        collection = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        pending_id = collection['new_pending_record_ids'][0]
        assert collection['new_case_refs'][0]['candidate_count'] == 1
        if early_return == 'capture_missing':
            # Simulate genuinely lost original evidence after capture, without
            # changing the retained pending case or creating a replacement.
            with runtime.store.locked() as db:
                db.execute('DELETE FROM proactive_decision_items WHERE decision_id=?', (decision_id,))
                db.execute('DELETE FROM proactive_decisions WHERE decision_id=?', (decision_id,))
                db.commit()
        reviews = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        row = next(result for result in reviews['review_results'] if result['pending_record_id'] == pending_id)
        expected_reason = ('pending_capture_decision_missing'
                           if early_return == 'capture_missing' else 'semantic_off_topic')
        assert row['reasons'] == [expected_reason]
        assert row['signal_checks_complete'] is False
        assert row['signal_counts']['delivered'] == row['signal_counts']['host_used'] == 0
        case = production_collection_diagnostics(collection, reviews)['new_case_reviews'][0]
        assert case['review_status'] == 'fail'
        assert case['signal_checks_complete'] is False
        assert case['candidate_count'] == 1  # The collector did observe this fact.
        assert case['signal_counts']['candidates'] == (None if early_return == 'capture_missing' else 1)
        assert all(case['signal_counts'][key] is None for key in (
            'delivered', 'semantic_relevant', 'verified_proof', 'host_used', 'host_rejected'))
        native_receipt = runtime.store.get_by_id(case['review_receipt_id'])
        assert native_receipt.content['pending_record_id'] == pending_id
        assert native_receipt.content['reasons'] == [expected_reason]


def test_duplicate_cohort_ids_are_deduplicated_and_conflicting_rows_remain_ambiguous():
    collection = _collection(_case(1), _case(1, 0), new_count=1)
    result = production_collection_diagnostics(collection, {'review_results': [
        _review(1), _review(1, review_receipt_id='prar_' + 'a' * 32,
                            review_verdict='pass', accepted_record_id='prqa_' + 'b' * 32)]})
    assert result['new_pending_record_ids'] == [_pending(1)]
    assert len(result['new_case_reviews']) == 1
    case = result['new_case_reviews'][0]
    assert case['review_status'] == 'ambiguous'
    assert case['candidate_count'] is None and case['capture_ref'] is None
    assert case['review_receipt_id'] is None and case['accepted_record_id'] is None
    assert case['review_complete'] is None and case['reasons'] is None


def test_native_collection_is_idempotent_and_reports_only_new_case_metadata(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _seed(runtime, 1300)
        _seed(runtime, 1301, with_candidate=False)
        first = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        assert (first['new_count'], first['existing_count'], first['new_nonempty_count'],
                first['new_empty_count']) == (2, 0, 1, 1)
        assert [case['candidate_count'] for case in first['new_case_refs']] == [1, 0]
        second = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        assert second['new_count'] == 0 and second['existing_count'] == 2
        assert second['new_nonempty_count'] == second['new_empty_count'] == 0
        assert second['new_pending_record_ids'] == second['new_case_refs'] == []
        reviewed = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        result = production_collection_diagnostics(second, reviewed)
        assert result['new_count'] == 0 and result['new_case_reviews'] == []


def test_native_nonempty_new_case_is_selected_before_the_fifty_id_report_cap(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _, decision_id = _seed(runtime, 1400)
        _flood(runtime, 52, cap=100, prefix='diagnostic-window-empty')
        report = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        assert report['new_count'] == 53
        assert report['new_nonempty_count'] == 1 and report['new_empty_count'] == 52
        assert len(report['new_pending_record_ids']) == len(report['new_case_refs']) == 50
        assert report['new_pending_record_ids_truncated'] is True
        assert report['new_case_refs'][0]['capture_ref'] == decision_id
        assert report['new_case_refs'][0]['candidate_count'] == 1
        assert report['new_pending_record_ids'] == [case['pending_record_id'] for case in report['new_case_refs']]
        result = production_collection_diagnostics(report, {})
        assert result['new_pending_record_ids_truncated'] is True
        assert result['new_case_reviews'][0]['capture_ref'] == decision_id
        assert result['new_case_reviews'][0]['candidate_count'] == 1


def test_native_reused_accepted_result_needs_no_new_receipt_and_is_not_failure(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _seed(runtime, 1500)
        collection = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        first = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert first['accepted_count'] == 1
        reused = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert reused['already_accepted_count'] == 1
        assert reused['receipt_count'] == reused['reviewed_count'] == 0
        result = production_collection_diagnostics(collection, reused)
        case = result['new_case_reviews'][0]
        assert case['review_status'] == 'already_accepted'
        assert case['review_receipt_id'] is None
        assert case['accepted_record_id'] == first['accepted_record_ids'][0]
        assert case['reasons'] == [] and case['review_complete'] is True


def test_native_new_case_to_review_to_durable_summary_survives_restart(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _seed(runtime, 1600)
        old_collection = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        old_pending = old_collection['new_pending_record_ids'][0]
        _, decision_id = _seed(runtime, 1601, state='not_used', proof=False)
        collection = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        assert collection['new_count'] == 1 and collection['existing_count'] == 1
        pending_id = collection['new_pending_record_ids'][0]
        reviews = jobs._run_production_recall_auto_review(runtime, scope=BASE_SCOPE)
        diagnostics = nightly_result_diagnostics({'production_recall_collection': collection,
            'production_recall_auto_review': reviews}, [])
        result = diagnostics['production_recall_collection']
        assert result['new_count'] == 1
        assert old_pending not in result['new_pending_record_ids']
        assert len(result['new_case_reviews']) == 1
        case = result['new_case_reviews'][0]
        assert case['pending_record_id'] == pending_id and case['capture_ref'] == decision_id
        assert case['candidate_count'] == 1
        assert case['review_status'] == 'fail'
        assert case['reasons'] == ['independent_signal_agreement_missing']
        assert case['signal_checks_complete'] is True
        assert case['signal_counts']['candidates'] == 1
        assert case['signal_counts']['delivered'] == case['signal_counts']['semantic_relevant'] == 1
        assert case['signal_counts']['host_used'] == case['signal_counts']['verified_proof'] == 0
        assert case['semantic_quality'] == {'status': 'evaluated', 'verdict': 'relevant'}
        native_review = runtime.store.get_by_id(case['review_receipt_id'])
        assert native_review.content['pending_record_id'] == pending_id
        assert native_review.content['does_not_certify_answer'] is True
        assert native_review.content['reasons'] == case['reasons']
        assert runtime.store.get_by_id(pending_id).content['capture_ref'] == decision_id
        assert runtime.store.get_by_id(pending_id).content['candidate_refs']
        summary = supervisor_summary(command='nightly', ok=True, duration_ms=1, memory_peak=0)
        summary['nightly_diagnostics'] = diagnostics
        saved_id = persist_supervisor_summary(runtime, scope=BASE_SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(saved_id).content['nightly_diagnostics'] == diagnostics
        persisted = build_supervisor_contract(runtime, scope=BASE_SCOPE)['runs']['nightly']['nightly_diagnostics']
        assert persisted['production_recall_collection'] == result
        assert runtime.store.get_by_id(case['review_receipt_id']).content['pending_record_id'] == pending_id
        assert runtime.store.get_by_id(pending_id).content['capture_ref'] == decision_id


def test_per_scope_producers_survive_final_diagnostics_and_restart(tmp_path, monkeypatch):
    import eimemory.evaluation.recall_agent_scopes as agents
    import eimemory.evaluation.hermes_profile_coverage as profiles
    scopes = [{**BASE_SCOPE, 'user_id': user} for user in ('first', 'second')]
    monkeypatch.setattr(agents, 'recall_agent_scopes', lambda *_: scopes)
    monkeypatch.setattr(profiles, 'load_topology', lambda: None)
    calls = []
    def produce(stage, scope):
        index = 1 if scope['user_id'] == 'first' else 2
        calls.append((scope['user_id'], stage))
        return {'collection': _collection(_case(index)),
                'monitor': {'ok': True, 'new_count': index},
                'auto_review': {'ok': True, 'review_results': [_review(index)],
                                'coverage': {'schema': 'production_recall_review_progress.v1',
                                             'cycle_remaining_count': index}},
                'eval': {'ok': True, 'sample_count': 0, 'quality_gate': {
                    'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete'}}}[stage]
    for stage, name in [('collection', '_run_production_recall_collection'),
                        ('monitor', '_run_semantic_relevance_monitor'),
                        ('auto_review', '_run_production_recall_auto_review'),
                        ('eval', '_run_production_recall_eval')]:
        monkeypatch.setattr(jobs, name, lambda *a, _stage=stage, **kw: produce(_stage, kw['scope']))
    with Runtime.create(root=tmp_path) as runtime:
        per_agent = jobs._run_recall_quality_per_agent(runtime, scope=BASE_SCOPE, primary_report={})
        assert calls == [(user, stage) for user in ('first', 'second')
                         for stage in ('collection', 'monitor', 'auto_review', 'eval')]
        report = {'recall_quality_per_agent': per_agent,
                  'production_recall_collection': _collection(),
                  'production_recall_auto_review': {'coverage': {
                      'schema': 'production_recall_review_progress.v1', 'cycle_remaining_count': 27}}}
        diagnostics = nightly_result_diagnostics(report, [])
        rows = diagnostics['natural_recall_quality_per_agent']['producer_reports']
        assert len(rows) == 2
        for index, row in enumerate(rows, 1):
            assert row['scope'] == scopes[index - 1]
            assert row['snapshot'] == 'after_primary_batch'
            assert row['production_recall_collection']['new_count'] == 1
            assert row['production_recall_collection']['new_case_reviews'][0]['review_receipt_id'] == _review(index)['review_receipt_id']
            assert row['recall_semantic_relevance']['new_count'] == index
            assert row['recall_label_auto_review']['coverage']['cycle_remaining_count'] == index
        assert diagnostics['production_recall_collection']['new_count'] == 0
        assert diagnostics['recall_label_auto_review']['coverage']['cycle_remaining_count'] == 27
        assert diagnostics['recall_producer_coverage']['primary_snapshot'] == 'before_per_agent_producers'
        summary = supervisor_summary(command='nightly', ok=True, duration_ms=1, memory_peak=0)
        summary['nightly_diagnostics'] = diagnostics
        saved = persist_supervisor_summary(runtime, scope=BASE_SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(saved).content['nightly_diagnostics'] == diagnostics
        assert build_supervisor_contract(runtime, scope=BASE_SCOPE)['runs']['nightly']['nightly_diagnostics'] == diagnostics


def test_native_per_agent_collection_review_and_durable_diagnostics(tmp_path, monkeypatch):
    import eimemory.evaluation.recall_agent_scopes as agents
    import eimemory.evaluation.hermes_profile_coverage as profiles
    monkeypatch.setattr(agents, 'recall_agent_scopes', lambda *_: [BASE_SCOPE])
    monkeypatch.setattr(profiles, 'load_topology', lambda: None)
    monkeypatch.setattr(jobs, '_run_semantic_relevance_monitor', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(jobs, '_run_production_recall_eval', lambda *a, **kw: {
        'ok': True, 'sample_count': 0, 'quality_gate': {
            'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete'}})
    with Runtime.create(root=tmp_path) as runtime:
        primary_collection = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
        primary_review = jobs._run_production_recall_auto_review(runtime, scope=BASE_SCOPE)
        _, decision_id = _seed(runtime, 1800, state='not_used', proof=False)
        result = jobs._run_recall_quality_per_agent(runtime,
            scope={**BASE_SCOPE, 'agent_id': 'scheduler'}, primary_report={})
        report = {'production_recall_collection': primary_collection,
                  'production_recall_auto_review': primary_review, 'recall_quality_per_agent': result}
        diagnostics = nightly_result_diagnostics(report, [])
        row = diagnostics['natural_recall_quality_per_agent']['producer_reports'][0]
        case = row['production_recall_collection']['new_case_reviews'][0]
        assert case['capture_ref'] == decision_id
        assert case['review_status'] == 'fail'
        assert case['reasons'] == ['independent_signal_agreement_missing']
        receipt_id = case['review_receipt_id']
        assert runtime.store.get_by_id(receipt_id).content['pending_record_id'] == case['pending_record_id']
        summary = supervisor_summary(command='nightly', ok=True, duration_ms=1, memory_peak=0)
        summary['nightly_diagnostics'] = diagnostics
        saved = persist_supervisor_summary(runtime, scope=BASE_SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(saved).content['nightly_diagnostics'] == diagnostics
        assert runtime.store.get_by_id(receipt_id).content['pending_record_id'] == case['pending_record_id']


def test_later_scope_exception_retains_earlier_producer_diagnostics_and_veto(monkeypatch):
    import eimemory.evaluation.recall_agent_scopes as agents
    import eimemory.evaluation.hermes_profile_coverage as profiles
    scopes = [{**BASE_SCOPE, 'user_id': user} for user in ('first', 'second')]
    monkeypatch.setattr(agents, 'recall_agent_scopes', lambda *_: scopes)
    monkeypatch.setattr(profiles, 'load_topology', lambda: None)
    monkeypatch.setattr(jobs, '_run_production_recall_collection', lambda *a, **kw: _collection(_case(1)))
    def monitor(*a, **kw):
        if kw['scope']['user_id'] == 'second':
            raise RuntimeError('fixture failure')
        return {'ok': True}
    monkeypatch.setattr(jobs, '_run_semantic_relevance_monitor', monitor)
    monkeypatch.setattr(jobs, '_run_production_recall_auto_review', lambda *a, **kw: {
        'ok': True, 'review_results': [_review(1)]})
    monkeypatch.setattr(jobs, '_run_production_recall_eval', lambda *a, **kw: {
        'ok': True, 'sample_count': 3, 'quality_gate': {'ok': True}})
    result = jobs._run_recall_quality_per_agent(object(), scope=BASE_SCOPE, primary_report={})
    assert result['ok'] is False
    assert jobs._per_agent_bounded_gate({'ok': True}, result)['ok'] is False
    diagnostics = nightly_result_diagnostics({'recall_quality_per_agent': result}, [])
    assert diagnostics['natural_recall_quality_per_agent']['status'] == 'blocked'
    rows = diagnostics['natural_recall_quality_per_agent']['producer_reports']
    assert len(rows) == 2
    assert rows[0]['production_recall_collection']['new_case_reviews'][0]['review_status'] == 'fail'
    assert rows[1]['production_recall_collection']['new_case_reviews'][0]['review_status'] == 'not_reported'
    assert rows[1]['recall_label_auto_review']['status'] == 'not_run'
