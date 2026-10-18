"""Real signed captures feed independent observations, never natural gold."""
from hashlib import sha256
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation import explicit_recall, semantic_relevance_monitor as monitor
from eimemory.evaluation.production_query_dataset import build_production_query_dataset
from eimemory.evaluation.real_query_schema import production_real_query_policy_payload
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _run_semantic_relevance_monitor
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_explicit_recall_capture import setup, BASE
from test_explicit_recall_private_capture import enable
from test_release_scope_binding import receipt_for_service, RELEASE


@pytest.fixture
def review_case(setup, monkeypatch):
    runtime, service, gold = setup
    enable(monkeypatch)
    runtime._test_runtime_commit = RELEASE.commit
    exact = ScopeRef.from_dict(resolve_channel_scope('codex', BASE))
    receipt = receipt_for_service()
    receipt.scope = exact
    runtime.store.append(receipt)

    def capture(index='one', *, complete=True, status='evidence_found', empty=False,
                acceptance=False, budget=0, extra=()):
        monkeypatch.setattr(runtime.memory, 'recall', lambda **kwargs: RecallBundle(
            items=[] if empty else [gold, *extra], rules=[], reflections=[], confidence=1,
            next_action_hint='', explanation={
                'relevance_selector': {'collection_complete': complete, 'status': status},
                'engine_diagnostics': {'source_budget_exhausted': budget, 'drops': {}},
            }))
        result = service.prefetch(channel='codex', scope=BASE,
            query='PRIVATE original Orchid service connection question ' + str(index),
            task_type='code.task', limit=3, task_context={'source_ids': ['shared']},
            explicit_request={'session_id': 'real-explicit-host', 'request_id': str(index),
                              'acceptance_generated': acceptance, 'capture_mode': 'private'})
        assert result['ok'] is True
        return result['capture']['record_id']

    return runtime, exact, gold, capture


def completion(labels, *, unanswered=False):
    return json.dumps(dict(relevance=labels, off_topic=all(label == 'unrelated' for label in labels),
                           duplicates=False, unanswered=unanswered))


def reviews(runtime, scope):
    return [record for record in runtime.store.list_records(kinds=['evaluation_packet'], scope=scope, limit=500)
            if record.source == monitor.EXPLICIT_REVIEW_SOURCE]


def test_real_private_capture_review_is_signed_cached_and_does_not_create_natural_gold(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture_id = capture()
    before = build_production_query_dataset(runtime, scope=BASE)
    calls = []

    def complete(_system, payload):
        payload = json.loads(payload)
        calls.append(payload)
        assert 'PRIVATE original' in payload['query']
        assert payload['items'][0]['text'].startswith('Orchid uses')
        return completion(['relevant'])

    monkeypatch.setattr(monitor, '_complete_tool_free', complete)
    first = monitor.monitor_explicit_recalls(runtime, scope=exact)
    second = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert first['new_count'] == first['provider_calls'] == first['verdict_counts']['relevant'] == 1
    assert second['reused_count'] == 1 and second['provider_calls'] == 0
    assert len(calls) == 1
    receipt = reviews(runtime, exact)[0]
    assert explicit_recall._verify(receipt, monitor.EXPLICIT_REVIEW_SOURCE, exact)['capture_ref'] == capture_id
    assert receipt.content['natural_benchmark_eligible'] is False
    assert receipt.content['memory_contribution_certified'] is False
    assert 'PRIVATE original' not in json.dumps(receipt.to_dict())
    after = build_production_query_dataset(runtime, scope=BASE)
    assert before['progress']['accepted_case_count'] == after['progress']['accepted_case_count'] == 0
    assert before['dataset']['cases'] == after['dataset']['cases'] == []
    assert production_real_query_policy_payload()['required_case_count'] == 3
    assert runtime.store.sqlite.conn.execute('SELECT COUNT(*) FROM proactive_decisions').fetchone()[0] == 0
    assert not runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=exact,
        meta_key='report_type', meta_value='production_recall_accepted_case', limit=500)


@pytest.mark.parametrize('mutation', ['capture_query', 'capture_result', 'physical_record', 'vault_query'])
def test_tampered_capture_or_current_authority_never_calls_model(review_case, monkeypatch, mutation):
    runtime, exact, gold, capture = review_case
    capture_id = capture()
    if mutation == 'physical_record':
        gold.summary = 'forged replacement memory'
        runtime.store.rewrite(gold, previous_scope=gold.scope)
    elif mutation == 'vault_query':
        with runtime.store.locked() as db:
            row = db.execute('SELECT payload FROM explicit_query_input_vault WHERE capture_id=?',
                             (capture_id,)).fetchone()
            payload = json.loads(row['payload'])
            payload['query'] = 'forged original'
            serialized = json.dumps(payload, sort_keys=True)
            db.execute('UPDATE explicit_query_input_vault SET payload=?,input_digest=? WHERE capture_id=?',
                       (serialized, sha256(serialized.encode()).hexdigest(), capture_id))
            db.commit()
    else:
        record = runtime.store.get_by_id(capture_id, scope=exact)
        if mutation == 'capture_query':
            record.content['request']['query_digest'] = '0' * 64
        else:
            record.content['result']['bundle']['items'] = []
        runtime.store.rewrite(record, previous_scope=record.scope)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unverified input'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['provider_calls'] == 0
    assert result['verdict_counts']['relevant'] == 0
    assert not reviews(runtime, exact)


@pytest.mark.parametrize('raw', ['{}', 'not JSON', completion(['unrelated']),
                              completion(['unknown'], unanswered='unknown')])
def test_malformed_or_unknown_judgment_does_not_certify_a_sample(review_case, monkeypatch, raw):
    runtime, exact, _, capture = review_case
    capture()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: raw)
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['verdict_counts']['unknown'] == 1
    assert result['natural_benchmark_eligible'] is False
    assert result['memory_contribution_certified'] is False


def test_transport_failure_is_redacted_and_can_be_retried(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture()
    def failed(*_):
        raise RuntimeError('private provider token')
    monkeypatch.setattr(monitor, '_complete_tool_free', failed)
    first = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert first['verdict_counts']['unknown'] == 1
    assert 'private provider token' not in json.dumps(first) + json.dumps(reviews(runtime, exact)[0].to_dict())
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: completion(['relevant']))
    second = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert second['new_count'] == second['provider_calls'] == 1
    assert second['verdict_counts']['relevant'] == 1


@pytest.mark.parametrize('verdict', ['off_topic', 'mixed'])
def test_negative_and_mixed_results_keep_their_independent_verdict(review_case, monkeypatch, verdict):
    runtime, exact, _, capture = review_case
    extra = []
    if verdict == 'mixed':
        extra.append(runtime.store.append(RecordEnvelope.create(
            kind='memory', title='Unrelated fixture', summary='The unrelated fixture describes a different service.',
            source='fixture.verified', source_id='shared', scope=ScopeRef.from_dict(BASE),
            meta={'memory_type': 'durable_fact', 'force_capture': True})))
    capture(extra=extra)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: completion(
        ['relevant', 'unrelated'] if verdict == 'mixed' else ['unrelated'],
        unanswered=verdict == 'off_topic'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['verdict_counts'][verdict] == result['provider_calls'] == 1
    assert result['natural_benchmark_eligible'] is False


def test_invalid_cached_review_cannot_be_reused_or_overwritten(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: completion(['relevant']))
    monitor.monitor_explicit_recalls(runtime, scope=exact)
    receipt = reviews(runtime, exact)[0]
    receipt.content['verdict'] = 'off_topic'
    runtime.store.rewrite(receipt, previous_scope=receipt.scope)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('tampered review'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['reason_counts'] == {'review_receipt_invalid': 1}
    assert result['provider_calls'] == 0


def test_unbound_release_is_excluded_before_review(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    runtime._test_runtime_commit = 'b' * 40
    capture()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unbound capture'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['reason_counts'] == {'release_unbound': 1}


@pytest.mark.parametrize('options,reason,verdict', [
    ({'complete': False}, 'retrieval_incomplete', 'excluded'),
    ({'complete': None}, 'retrieval_completeness_unknown', 'unknown'),
    ({'status': 'degraded'}, 'retrieval_incomplete', 'excluded'),
    ({'budget': 1}, 'retrieval_incomplete', 'excluded'),
    ({'acceptance': True}, 'maintenance_capture', 'excluded'),
])
def test_incomplete_and_maintenance_captures_do_not_reach_judge(review_case, monkeypatch, options, reason, verdict):
    runtime, exact, _, capture = review_case
    capture(**options)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('ineligible'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['provider_calls'] == 0
    assert result['verdict_counts'][verdict] == result['reason_counts'][reason] == 1


def test_opted_out_private_input_is_unknown_without_loading_text(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    capture()
    monkeypatch.setattr(explicit_recall, 'load_explicit_capture_review_input',
                        lambda *_args, **_kwargs: pytest.fail('opted-out input loaded'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact)
    assert result['reason_counts'] == {'capture_input_unavailable': 1}
    assert result['verdict_counts']['unknown'] == 1


def test_empty_results_do_not_spend_the_model_budget(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture('useful')
    for index in range(12):
        capture('empty-' + str(index), empty=True, status='no_evidence')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: completion(['relevant']))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact, max_new=1)
    assert result['provider_calls'] == result['verdict_counts']['relevant'] == 1
    assert result['verdict_counts']['excluded'] == result['reason_counts']['empty_result'] == 12
    assert result['deferred_count'] == 0


def test_source_authorization_precedes_private_and_cached_reads(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture()
    monkeypatch.setattr(explicit_recall, 'load_explicit_capture',
                        lambda *_args, **_kwargs: pytest.fail('ungranted capture loaded'))
    result = monitor.monitor_explicit_recalls(runtime, scope=exact, source_ids={'other'})
    assert result['provider_calls'] == 0
    assert result['reason_counts'] == {'source_not_authorized': 1}


def test_explicit_only_agent_is_visible_in_nightly_but_not_natural_counts(review_case, monkeypatch):
    runtime, exact, _, capture = review_case
    capture()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: completion(['relevant']))
    operator = dict(BASE, agent_id='operator', workspace_id='operations', user_id='operator')
    report = _run_semantic_relevance_monitor(runtime, scope=operator)
    assert report['ok'] is True and report['new_count'] == 0
    assert report['explicit']['by_agent'][exact.agent_id]['verdict_counts']['relevant'] == 1
    diagnostic = nightly_result_diagnostics({'semantic_relevance_monitor': report}, [])
    explicit = diagnostic['explicit_recall_review']
    assert explicit['by_agent'][exact.agent_id]['verdict_counts']['relevant'] == 1
    assert explicit['natural_benchmark_eligible'] is explicit['memory_contribution_certified'] is False
    assert diagnostic['recall_semantic_relevance']['verdict_counts'] == {}
    assert 'PRIVATE original' not in json.dumps(diagnostic)


def test_nightly_explicit_diagnostics_reject_arbitrary_text_and_invalid_references():
    report = monitor._explicit_counts()
    report.update(status=['PRIVATE'], by_agent={'PRIVATE /path': report.copy()},
                  reason_counts={'PRIVATE provider content': 1},
                  review_results=[dict(capture_record_id='PRIVATE', review_receipt_id='/private/path',
                                      verdict='PRIVATE', reason='PRIVATE')])
    diagnostic = nightly_result_diagnostics({'semantic_relevance_monitor': {'explicit': report}}, [])
    text = json.dumps(diagnostic['explicit_recall_review'])
    assert 'PRIVATE' not in text and '/private' not in text
    assert diagnostic['explicit_recall_review']['review_results'][0]['capture_record_id'] is None
