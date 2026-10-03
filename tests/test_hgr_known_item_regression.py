"""Frozen incident query plus fixed controls; not natural-quality certification."""
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.retrieval.answer_requirements import requested_attribute
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text

HGR = json.loads((Path(__file__).parent / 'fixtures/hgr_known_item.json').read_text())
REWRITE = 'HGR如何把分子的高阶拓扑编码成产生式规则序列，避免标准序列表示的局限？'


@pytest.mark.parametrize('query', [HGR['query'], REWRITE,
    '标准序列模型与分子图表示',
    '条件扩散模型通过约束满足生成有效分子。',
    'The model uses standard representations and production rules.',
])
def test_research_mentions_are_not_constraint_requests(query):
    assert requested_attribute(query) == ''


@pytest.mark.parametrize('query', [
    'Atlas项目验收标准是什么？', 'Atlas项目上线需要满足什么条件？',
    'Atlas项目的约束', '请列出Atlas项目的要求',
    'What are the requirements for project Atlas?',
    'project Atlas acceptance requirements?',
])
def test_actual_constraint_requests_remain_constraints(query):
    assert requested_attribute(query) == 'constraint'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_POSTGRES_VECTOR_ENABLED', '0')
    monkeypatch.setenv('EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED', '0')
    runtime = Runtime.create(root=tmp_path, profile='core')
    scope = ScopeRef(user_id='hgr-control')
    target = RecordEnvelope.create(kind='knowledge_page', title=HGR['title'],
        summary=HGR['text'], detail=HGR['text'], content={'text': HGR['text']}, scope=scope)
    target = replace(target, record_id=HGR['expected_record_id'])
    runtime.store.append(target)
    for title, text in [
        ('same-topic', '分子图模型学习环系和模体，但需要高阶拓扑编码。标准图表示采用邻接矩阵。'),
        ('cross-topic', '信息检索正变得越来越重要，因为 LLM 智能体需要处理复杂任务。标准索引表示通过 SELF-INDEX 优化。'),
    ]:
        runtime.store.append(RecordEnvelope.create(kind='knowledge_page', title=title,
            summary=text, content={'text': text}, scope=scope))
    # Identical content outside each authority boundary must never be admitted.
    runtime.store.append(replace(target, record_id='foreign-user', scope=replace(scope, user_id='other')))
    runtime.store.append(replace(target, record_id='foreign-source', source_id='private'))
    yield runtime, scope
    runtime.close()


@pytest.mark.parametrize('query', [HGR['query'], REWRITE])
def test_original_and_rewrite_with_fixed_distractors_and_isolation(runtime, query):
    runtime, scope = runtime
    result = runtime.memory.recall(query=query, scope=asdict(scope),
        task_context={'exact_scope_only': True, 'source_ids': ['default'], 'kinds': ['knowledge_page']}, limit=5)
    ids = [r.record_id for r in result.items]
    assert ids and ids[0] == HGR['expected_record_id']
    assert 'foreign-user' not in ids and 'foreign-source' not in ids
    assert 'cross-topic' not in [r.title for r in result.items]


@pytest.mark.parametrize('query', [HGR['query'], REWRITE])
def test_shared_attribute_check_in_fragment_admission(query):
    # Synthetic cosine only verifies the shared boundary, not vector quality.
    record = RecordEnvelope.create(kind='knowledge_page', title=HGR['title'],
        summary=HGR['text'], scope=ScopeRef())
    fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
    selected, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [record], query=query, limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: dict(dense_vector_score=.95, fragment_policy=POLICY,
                                evidence_fragment_id=fragment['id']))
    assert selected == [record]
    assert report['requested_attribute'] == ''


def test_no_answer_and_deny_all_are_not_filled(runtime):
    runtime, scope = runtime
    for query, sources in [('HGR分子表示的许可证价格是多少钱？', ['default']), (HGR['query'], [])]:
        result = runtime.memory.recall(query=query, scope=asdict(scope),
            task_context={'exact_scope_only': True, 'source_ids': sources}, limit=5)
        assert result.items == []


def test_false_recall_keeps_legacy_value_and_exposes_distinct_failures():
    record = RecordEnvelope.create(kind='knowledge_page', title='unrelated', scope=ScopeRef())
    bundle = RecallBundle(items=[record], rules=[], reflections=[], confidence=1, next_action_hint='')
    runtime = SimpleNamespace(memory=SimpleNamespace(recall=lambda **kw: bundle))
    report = run_production_recall_eval(runtime, {'cases': [
        {'query': 'known item', 'expected_record_ids': ['missing']},
        {'query': 'no answer', 'no_answer': True},
    ]}, seed=False)
    assert report['false_recall_rate'] == 1.0
    assert report['false_recall_breakdown'] == {
        'known_item_miss_with_return_count': 1, 'no_answer_return_count': 1,
        'no_answer_sample_count': 1,
    }
    assert report['quality_gate']['blocking_metrics']['false_recall_rate']['actual'] == 1.0
    assert report['samples'][0]['false_recall_reason'] == 'known_item_miss_with_return'
    assert report['samples'][1]['false_recall_reason'] == 'no_answer_return'
