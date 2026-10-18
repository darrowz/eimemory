"""Replay must reject lost or contradictory original retrieval boundaries."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.evaluation import semantic_gap_repair as repair
from eimemory.evaluation.query_input_vault import load_query_input
from eimemory.models.records import RecallBundle, RecordEnvelope
from test_semantic_gap_repair import incident
from test_semantic_gap_repair_flow import replay_case, verdict


def _vault_row(runtime, decision_id):
    with runtime.store.locked() as db:
        return dict(db.execute(
            'SELECT payload,input_digest FROM proactive_query_input_vault WHERE decision_id=?',
            (decision_id,),
        ).fetchone())


def _replace_stored_context(runtime, decision_id, context):
    """Model a stored invalid context with an otherwise valid input identity."""
    payload = json.loads(_vault_row(runtime, decision_id)['payload'])
    payload['task_context'] = context
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with runtime.store.locked() as db:
        db.execute(
            'UPDATE proactive_query_input_vault SET payload=?,input_digest=? WHERE decision_id=?',
            (serialized, sha256(serialized.encode()).hexdigest(), decision_id),
        )
        db.commit()


@pytest.mark.parametrize('defect', [
    'empty_context', 'foreign_source', 'relaxed_scope', 'different_task_type',
    'missing_runtime_channel', 'invalid_runtime_channel', 'foreign_target_source',
    'unknown_context_key', 'canonical_first_strategy', 'legacy_union_strategy',
    'integer_scope_flag',
])
def test_invalid_original_context_is_unverifiable_without_retrieval_or_judge(incident, defect):
    runtime, scope, gap, _old, _record, decision_id = incident
    context = json.loads(_vault_row(runtime, decision_id)['payload'])['task_context']
    if defect == 'empty_context':
        context = {}
    elif defect == 'foreign_source':
        context['source_ids'] = ['foreign']
    elif defect == 'relaxed_scope':
        context['exact_scope_only'] = False
    elif defect == 'different_task_type':
        context['task_type'] = 'memory.recall'
    elif defect == 'missing_runtime_channel':
        context.pop('runtime_channel')
    elif defect == 'invalid_runtime_channel':
        context['runtime_channel'] = 'foreign-host'
    elif defect == 'foreign_target_source':
        context['target_source_id'] = 'foreign'
    elif defect == 'canonical_first_strategy':
        context['scope_strategy'] = 'canonical_first'
    elif defect == 'legacy_union_strategy':
        context['scope_strategy'] = 'legacy_union'
    elif defect == 'integer_scope_flag':
        context['exact_scope_only'] = 1
    else:
        context['unexpected_retrieval_boundary'] = 'unverified'
    _replace_stored_context(runtime, decision_id, context)
    before = _vault_row(runtime, decision_id)
    with runtime.store.locked() as db:
        record_count = db.execute('SELECT count(*) FROM records').fetchone()[0]
    result = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    assert result['status'] == 'unverifiable'
    assert result['natural_benchmark_eligible'] is False
    assert result['passing_resolution_created'] is False
    assert _vault_row(runtime, decision_id) == before
    with runtime.store.locked() as db:
        assert db.execute('SELECT count(*) FROM records').fetchone()[0] == record_count
    assert runtime.store.get_by_id(gap.record_id).status == 'active'
    assert 'PRIVATE' not in json.dumps(result)


def test_valid_original_context_and_input_digest_remain_unchanged(incident):
    runtime, scope, gap, _old, _record, decision_id = incident
    before = _vault_row(runtime, decision_id)
    payload = json.loads(before['payload'])
    assert payload['task_context'] == {
        'source_ids': ['hermes'], 'runtime_channel': 'proactive',
        'task_type': 'research.task', 'exact_scope_only': True,
    }
    result = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    assert result['status'] == 'ready'
    assert result['input_digest'] == before['input_digest']
    assert _vault_row(runtime, decision_id) == before
    original_scope = runtime.store.load_proactive_decision(decision_id)['scope']
    inputs = load_query_input(runtime, decision_id=decision_id, scope=original_scope,
                              channel='hermes', source_id='hermes')
    assert inputs['task_context'] == payload['task_context']
    assert inputs['input_digest'] == before['input_digest']


def test_missing_decision_retention_pin_prevents_recall_and_capture(replay_case, monkeypatch):
    runtime, scope, gap, _old, _current, calls, judges, _options = replay_case
    pin_requests = []

    def missing_pin(decision_ids, *, reason):
        pin_requests.append((decision_ids, reason))
        return {'decision_missing_count': 1}

    monkeypatch.setattr(runtime.store, 'pin_proactive_decisions', missing_pin)
    with pytest.raises(ValueError, match='semantic_repair_decision_unavailable'):
        repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope,
            _on_recall=lambda: pytest.fail('missing decision consumed a recall call'))
    assert pin_requests == [(['pd:' + '1' * 32], 'pending_case')]
    assert calls == judges == []
    with runtime.store.locked() as db:
        assert db.execute('SELECT count(*) FROM records WHERE source=?',
                          (repair.CAPTURE_SOURCE,)).fetchone()[0] == 0


def test_failed_post_judge_verification_still_consumes_budget_and_defers_next_packet(replay_case, monkeypatch):
    runtime, scope, gap, _old, current_a, calls, judges, _options = replay_case
    packet_a = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    current_b = RecordEnvelope.create(kind='memory', title='Second current answer',
        summary='PRIVATE second current answer', scope=scope, source='hermes.memory',
        source_id='hermes', meta={'force_capture': True, 'memory_type': 'durable_fact'})
    runtime.store.append(current_b)

    def recall_b(**kwargs):
        calls.append(deepcopy(kwargs))
        return RecallBundle(items=[current_b], rules=[], reflections=[], confidence=.9,
            next_action_hint='', explanation={'retrieval_status': 'evidence_found',
                'relevance_selector': {'collection_complete': True, 'status': 'evidence_found'},
                'engine_diagnostics': {'source_budget_exhausted': 0,
                    'drops': {'recall_budget_exhausted': 0}}})

    monkeypatch.setattr(runtime.memory, 'recall', recall_b)
    packet_b = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope,
                                                  attempt_id='attemptB')
    assert packet_a['packet_ref'] != packet_b['packet_ref']
    for packet, current in ((packet_a, current_a), (packet_b, current_b)):
        stored = runtime.store.get_by_id(packet['packet_ref'], scope=scope)
        assert [ref['record_ref'] for ref in stored.content['references']] == [current.record_id]

    records_by_title = {record.title: record for record in (current_a, current_b)}

    def invalidate_judged_record(query, items):
        judges.append((query, deepcopy(items)))
        changed = records_by_title[items[0]['title']]
        changed.summary += ' changed during judgment'
        runtime.store.rewrite(changed, previous_scope=scope)
        return verdict()

    monkeypatch.setattr(monitor, '_evaluate', invalidate_judged_record)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=1)
    assert len(judges) == report['provider_calls'] == 1
    assert len(calls) == 2 and report['replay_count'] == 0
    assert report['unverifiable_count'] == report['deferred_count'] == 1
    assert report['reason_counts']['semantic_repair_result_authority_changed'] == 1
    assert findings == []
    assert runtime.store.get_by_id(gap.record_id).status == 'active'
