"""Local selector -> bundle -> proactive delivery; model responses are synthetic."""
from dataclasses import asdict
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from test_proactive_recall import BASE_SCOPE, _record, _service
from eimemory.models.records import RecallBundle
from eimemory.retrieval import caller_assistance
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.relevance import record_digest


@pytest.mark.parametrize('case', [
    'supported', 'topical', 'quote_only', 'cross_record', 'external',
    'revoked', 'cross_scope', 'no_answer', 'unavailable', 'duplicate',
    'ambiguous_id', 'ambiguous_scope', 'tail', 'summary', 'wrong_span',
    'changed', 'budget', 'cross_window', 'changed_store', 'replay_changed', 'malformed_high',
])
def test_proof_delivery(tmp_path, monkeypatch, case):
    query = '用户怎么称呼'
    quote = '用户称呼为小林。'
    record = _record('用户称呼相关资料。' if case == 'quote_only' else quote)
    if case in {'tail', 'wrong_span', 'changed', 'budget', 'cross_window', 'changed_store', 'replay_changed'}:
        record.title = record.summary = '无关资料'
        record.content['text'] = '日常天气记录。' * 400 + quote
    if case == 'summary':
        record.title = record.summary = '无关资料'
        record.content = {}
        record.detail = '日常天气记录。' * 400 + quote
    runtime, engine, service = _service(tmp_path, [record])
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_INDEPENDENT_EVIDENCE_MODE', 'off')
    selected = [] if case in {'topical', 'no_answer'} else [{'id': '0', 'quote': quote}]
    if case == 'quote_only':
        selected = [{'id': '0', 'quote': record.content['text']}]
    def complete(**kwargs):
        if case == 'unavailable':
            raise RuntimeError('synthetic unavailable')
        return SimpleNamespace(text=json.dumps({'selected': selected}))
    monkeypatch.setattr(caller_assistance, 'configured_client', lambda: SimpleNamespace(
        timeout_seconds=1, complete=complete))
    selector = GovernedRecallEngine(store=runtime.store, candidate_source=None)
    selector.relevance_admission = None
    def unchanged(item):
        current = runtime.store.get_by_exact_ref(item.record_id,
            scope=asdict(item.scope), source_id=item.source_id)
        return (current is not None and current.status == 'active'
                and record_digest(current) == record_digest(item))
    def recall(**kwargs):
        items, state = selector._select_post_fusion_items(
            [record], query=query, limit=3, fusion_state={}, component_hints_by_ref={},
            validate=unchanged,
        )
        if case not in {'topical', 'quote_only', 'no_answer', 'unavailable'}:
            assert len(items) == 1 and state['caller_assistance']['proofs']
        else:
            assert not items
        if case == 'cross_record':
            state['caller_assistance']['proofs'][0]['record_id'] = 'another-record'
        if case == 'malformed_high':
            state['caller_assistance']['proofs'][0]['span_end'] = 20000
            monkeypatch.setattr(service, 'eligible', lambda confidence: True)
        if case == 'wrong_span':
            state['caller_assistance']['proofs'][0].update(span_start=0, span_end=len(quote))
        if case == 'cross_window':
            from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
            text = candidate_record_keyword_text(record, max_text_chars=16000)
            proof = state['caller_assistance']['proofs'][0]
            proof.update(span_start=740, span_end=790,
                         quote_digest=sha256(text[740:790].encode()).hexdigest())
        if case == 'changed_store':
            changed = deepcopy(record)
            changed.content['text'] += '已失效。'
            runtime.store.append(changed)
        if case == 'changed':
            record.content['text'] += '已失效。'
        if case == 'budget':
            service.max_context_chars = 160
        if case == 'revoked':
            record.status = 'revoked'
        if case == 'cross_scope':
            record.scope.workspace_id = 'elsewhere'
        if case in {'ambiguous_id', 'ambiguous_scope'}:
            other = _record(quote, source_id='beta' if case == 'ambiguous_id' else 'alpha')
            other.record_id = record.record_id
            if case == 'ambiguous_scope':
                other.scope.workspace_id = 'elsewhere'
            items.append(other)
        return RecallBundle(items=items, rules=[], reflections=[], confidence=0,
            next_action_hint='', explanation={'relevance_selector': state,
                'fusion': {'selected': [{'record_id': record.record_id,
                    'source_id': record.source_id, 'evidence': ['vector_match']}]},
                'scoring': [{'record_id': record.record_id, 'source_id': record.source_id,
                             'quality_score': 0.1}]})
    monkeypatch.setattr(service, '_recall_with_timeout', recall)
    request = dict(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
                   session_id='proof-session', query_id='first', query=query)
    if case == 'external':
        request['recall_bundle'] = recall()
    decision = service.decide(**request)
    if case in {'supported', 'duplicate', 'tail', 'summary', 'replay_changed'}:
        assert len(decision['items']) == 1
        assert quote in decision['context']
        assert decision['items'][0]['confidence'] < 0.70
        stored = runtime.store.load_proactive_decision(decision['decision_id'])
        assert [item['record_id'] for item in stored['items']] == [record.record_id]
        assert quote not in json.dumps(stored, ensure_ascii=False)
        assert query not in json.dumps(stored, ensure_ascii=False)
        replay = service.decide(**request)
        assert quote in replay['context']
        if case == 'replay_changed':
            changed = deepcopy(record)
            changed.content['text'] += '已失效。'
            runtime.store.append(changed)
            replay = service.decide(**request)
            assert not replay['items'] and not replay['context']
        if case == 'duplicate':
            again = service.decide(**{**request, 'query_id': 'second'})
            assert not again['items'] and not again['context']
    else:
        assert not decision['items'] and not decision['context']
