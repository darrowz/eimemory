"""Reason counters retain bounded native receipt links through durable nightly summaries."""
import json

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_production_query_auto_review import (
    BASE_SCOPE, _keys, _seed, _collect, auto_review_pending_production_queries,
)


def _result(index, reason='no_candidate_refs'):
    return {'pending_record_id': 'prqp_' + f'{index:032x}',
            'review_receipt_id': 'pret_' + f'{index:032x}',
            'review_verdict': 'fail', 'reasons': [reason]}


def _project(report):
    return nightly_result_diagnostics({'production_recall_auto_review': report}, [])[
        'recall_label_auto_review']['not_passed_review_references']


def test_rare_reason_survives_common_reason_reference_cap_without_changing_counts():
    results = [_result(i) for i in range(135)] + [_result(999, 'semantic_off_topic')]
    report = {'review_results': results, 'reason_counts': {
        'not_passed': {'no_candidate_refs': 135, 'semantic_off_topic': 1}}}
    review = nightly_result_diagnostics({'production_recall_auto_review': report}, [])['recall_label_auto_review']
    assert review['not_passed_reason_counts'] == report['reason_counts']['not_passed']
    refs = review['not_passed_review_references']['reason_refs']
    assert refs['no_candidate_refs']['reference_count'] == 5
    assert refs['no_candidate_refs']['observed_result_count'] == 135
    assert refs['no_candidate_refs']['references_truncated'] is True
    assert refs['semantic_off_topic']['refs'] == [{
        key: results[-1][key] for key in ('pending_record_id', 'review_receipt_id')}]
    assert refs['semantic_off_topic']['references_truncated'] is False


def test_missing_legacy_refs_and_invalid_ids_are_not_reconstructed_or_leaked():
    assert _project({'ok': True})['status'] == 'not_reported'
    assert _project({'ok': True})['results_scanned_count'] is None
    result = _result(1)
    result.update(review_receipt_id='private-token', reasons=['semantic_off_topic', 'private-token'],
                  query='private-query', detail='private-body')
    projected = _project({'review_results': [result, {'private': 'private-token'}]})
    for code in ('semantic_off_topic', 'reason_not_allowlisted'):
        assert projected['reason_refs'][code]['missing_reference_count'] == 1
        assert projected['reason_refs'][code]['refs'] == []
    assert 'private' not in json.dumps(projected)


def test_duplicate_multi_reason_results_and_projection_limits_remain_explicit():
    first = _result(1)
    first['reasons'] = ['semantic_off_topic', 'semantic_off_topic', 'no_candidate_refs']
    passed = {**_result(2), 'review_verdict': 'pass'}
    projected = _project({'review_results': [first, first, passed]})
    for code in ('semantic_off_topic', 'no_candidate_refs'):
        bucket = projected['reason_refs'][code]
        assert bucket['observed_result_count'] == 2
        assert bucket['reference_count'] == 1 and bucket['references_truncated'] is False
    overlong = _result(3)
    overlong['reasons'] = ['no_candidate_refs'] * 101
    bounded = _project({'review_results': [overlong] + [_result(i) for i in range(1500)]})
    assert bounded['results_scanned_count'] == 1500 and bounded['results_truncated'] is True
    assert bounded['result_reasons_truncated'] is True
    assert bounded['reason_refs']['no_candidate_refs']['reference_count'] == 5


def test_native_off_topic_receipt_survives_nightly_persistence_and_restart(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _record, decision_id = _seed(runtime, 990, semantic='unrelated')
        pending_id = _collect(runtime)[0]
        reviews = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert reviews['reason_counts']['not_passed']['semantic_off_topic'] == 1
        diagnostics = nightly_result_diagnostics({'production_recall_auto_review': reviews}, [])
        refs = diagnostics['recall_label_auto_review']['not_passed_review_references']
        witness = refs['reason_refs']['semantic_off_topic']['refs'][0]
        assert witness['pending_record_id'] == pending_id
        native_receipt = runtime.store.get_by_id(witness['review_receipt_id'])
        assert native_receipt.content['pending_record_id'] == pending_id
        assert native_receipt.content['reasons'] == ['semantic_off_topic']
        assert runtime.store.get_by_id(pending_id).content['capture_ref'] == decision_id
        summary = supervisor_summary(command='nightly', ok=True, duration_ms=1, memory_peak=0)
        summary['nightly_diagnostics'] = diagnostics
        saved = persist_supervisor_summary(runtime, scope=BASE_SCOPE, summary=summary)
        receipt_id = saved.record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(receipt_id).content['nightly_diagnostics'] == diagnostics
        assert build_supervisor_contract(runtime, scope=BASE_SCOPE)['runs']['nightly'][
            'nightly_diagnostics'] == diagnostics
        assert runtime.store.get_by_id(witness['review_receipt_id']).content['pending_record_id'] == pending_id
