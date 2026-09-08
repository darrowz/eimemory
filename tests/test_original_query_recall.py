from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_dataset import accept_pending_production_query, collect_pending_production_queries
from test_production_query_dataset import BASE_SCOPE, LABEL_PACKET_EVIDENCE, _seed_decision


def prepared(tmp_path):
    runtime = Runtime.create(root=tmp_path / 'runtime')
    gold = _seed_decision(runtime, channel='codex', index=71)
    runtime.store.sqlite.conn.execute('DELETE FROM proactive_decision_items WHERE decision_id=?', ('decision-codex-71',))
    runtime.store.sqlite.conn.commit()
    pending = collect_pending_production_queries(runtime, scope=BASE_SCOPE)['pending_record_ids'][0]
    accepted = accept_pending_production_query(runtime, pending_record_id=pending,
        query_features={'terms':['archive','routing','destination'], 'intent':'memory recall'},
        labels=[{'record_ref':gold.record_id,'grade':3}], labeler='operator',
        operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)
    return runtime, gold, {'channel':'codex','accepted_record_id':accepted['record_id'], 'query':'raw secret query codex 71'}


def test_original_query_is_exact_and_observed_failure_is_not_overwritten(tmp_path, monkeypatch):
    from eimemory.evaluation.original_query_recall import evaluate_original_queries
    runtime, gold, case = prepared(tmp_path)
    calls = []
    def recall(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(items=[gold])
    monkeypatch.setattr(runtime.memory, 'recall', recall)
    report = evaluate_original_queries(runtime, scope=BASE_SCOPE, cases=[case])
    assert report['ok'] is True
    assert calls[0]['query'] == case['query']
    assert 'recall_profile' not in calls[0]['task_context']
    assert report['samples'][0]['observed']['recall_at_5'] == 0
    assert report['samples'][0]['rerun']['recall_at_5'] == 1
    assert report['natural_gate_replacement'] is False
    assert report['evaluator_version']
    assert report['engine_identity']
    assert report['samples'][0]['unavailable'] is False
    assert report['online_context_reconstructed'] is False
    assert case['query'] not in json.dumps(report)
    runtime.close()


@pytest.mark.parametrize('change', ['rewrite','other_scope','duplicate'])
def test_original_query_refuses_unverifiable_input_before_recall(tmp_path, monkeypatch, change):
    from eimemory.evaluation.original_query_recall import evaluate_original_queries
    runtime, gold, case = prepared(tmp_path)
    if change == 'rewrite':
        case['query'] = 'archive routing destination'
    if change == 'other_scope':
        case['channel'] = 'hermes'
    cases = [case, case] if change == 'duplicate' else [case]
    monkeypatch.setattr(runtime.memory, 'recall', lambda **_: pytest.fail('invalid packet reached recall'))
    with pytest.raises(ValueError):
        evaluate_original_queries(runtime, scope=BASE_SCOPE, cases=cases)
    runtime.close()
