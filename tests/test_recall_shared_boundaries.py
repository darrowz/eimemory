"""Admission boundary tests; controlled scores test plumbing, not model quality."""
from copy import deepcopy

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
from eimemory.retrieval.relevance import RelevanceAdmission, RelevanceConfig, authoritative_text


class PositiveScores:
    def score(self, query, texts, **kwargs):
        return [1.0] * len(texts)


def record(text):
    return RecordEnvelope.create(kind='memory', title='article procedure', summary=text,
        detail=text, content={'text': text}, scope=ScopeRef(user_id='synthetic-boundary'),
        source='synthetic.fixture', source_id='boundary')


def select(items, mode):
    if mode == 'reranker':
        return RelevanceAdmission(RelevanceConfig(), PositiveScores()).select(
            items, query='article procedure', limit=5, validate=lambda _: True)
    def hints(item):
        fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
        return {'dense_vector_score': .8, 'fragment_policy': POLICY,
                'evidence_fragment_id': fragment['id']}
    return LightweightAdmission(LightweightConfig(enabled=True)).select(
        items, query='article', limit=5, validate=lambda _: True,
        hints_for=hints, backend_available=True)


@pytest.mark.parametrize('mode', ['reranker', 'lightweight'])
@pytest.mark.parametrize('difference', ['version', 'source', 'scope', 'source_id', 'payload', 'event_time'])
def test_admission_dedup_keeps_distinct_authoritative_records(mode, difference):
    first = record('Read article before deployment.')
    second = deepcopy(first)
    second.record_id += '_second'
    if difference == 'version':
        first.meta = {'version': 'v1.2'}
        second.meta = {'version': 'v1.3'}
    elif difference == 'source':
        second.source = 'synthetic.other'
    elif difference == 'scope':
        second.scope.user_id = 'other'
    elif difference == 'source_id':
        second.source_id = 'other'
    elif difference == 'payload':
        second.content['condition'] = 'only after approval'
    else:
        first.meta = second.meta = {'memory_type': 'event'}
        second.time.occurred_at = '2099-01-01T00:00:00Z'
    chosen, _ = select([first, second], mode)
    assert {item.record_id for item in chosen} == {first.record_id, second.record_id}


def test_lightweight_dedup_preserves_ordered_procedures():
    first = record('article procedure: start motor then open valve')
    second = record('article procedure: open valve then start motor')
    chosen, _ = select([first, second], 'lightweight')
    assert len(chosen) == 2


def test_reranker_projection_collision_does_not_erase_middle_evidence():
    first = record('article ' * 100 + 'APPROVED' + ' article' * 100)
    second = record('article ' * 100 + 'REJECTED' + ' article' * 100)
    assert authoritative_text(first, max_chars=700) == authoritative_text(second, max_chars=700)
    chosen, _ = select([first, second], 'reranker')
    assert len(chosen) == 2


@pytest.mark.parametrize('mode', ['reranker', 'lightweight'])
def test_admission_still_collapses_full_duplicate(mode):
    first = record('Read article before deployment.')
    second = deepcopy(first)
    second.record_id += '_copy'
    chosen, _ = select([first, second], mode)
    assert len(chosen) == 1


@pytest.mark.parametrize('explicit', [False, True])
def test_operational_permission_cannot_hide_external_source_safety(tmp_path, explicit):
    from contextlib import closing
    from eimemory.api.memory import MemoryAPI
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.knowledge.safety import evaluate_knowledge_safety
    item = RecordEnvelope.create(kind='knowledge_unit', title='deployment procedure',
        source='deployment', scope=ScopeRef(user_id='synthetic-boundary'),
        content={'text': 'Deployment process reference'})
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        assert not evaluate_knowledge_safety(item, task='recall', registry=api.source_registry)['recall_allowed']
        chosen, dropped = api._apply_online_recall_pollution_gate(
            [item], allow_operational_recall=True, explicit_evidence_boundary=explicit)
        assert chosen == []
        assert dropped['external_knowledge_untrusted'] == 1
