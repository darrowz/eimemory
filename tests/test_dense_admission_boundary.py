"""Controlled measurements exercise admission, not embedding quality."""
import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.caller_assistance import needs_verification


@pytest.mark.parametrize('query', [
    '用户的火星基地门禁口令是什么',
    '用户的月球仓库门禁口令是什么',
    'User的火星基地门禁口令是什么',
])
@pytest.mark.parametrize('cosine', [.3, .7, .99])
def test_dense_similarity_alone_is_not_answer_evidence(query, cosine):
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine._relevance_selector_thresholds = {'non_exact_min_grounding': .08}
    item = RecordEnvelope.create(kind='memory', title='恢复偏好',
        summary='网关重启后，先只读复核前序已授权任务，再问下一步。',
        content={}, scope=ScopeRef())
    score, _ = engine._non_exact_grounding_score(query=query, item=item,
        evidence={'vector_match'}, component_hints_by_ref={engine._record_key(item): {
            'dense_vector_score': cosine, 'vector_score': cosine}},
        graph_grounded_ids=set(), vector_min_score=.12, explicit_recall_boundary=False)
    assert score < .08


def test_requested_attribute_applies_to_default_grounding():
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine._relevance_selector_thresholds = {'non_exact_min_grounding': .08}
    item = RecordEnvelope.create(kind='memory', title='Atlas项目合同',
        summary='Atlas项目合同已签署，金额尚未确定。', content={}, scope=ScopeRef())
    score, reason = engine._non_exact_grounding_score(query='Atlas项目合同总金额是多少',
        item=item, evidence={'keyword_exact'}, component_hints_by_ref={},
        graph_grounded_ids=set(), vector_min_score=.12, explicit_recall_boundary=False)
    assert score == 0
    assert reason == 'requested_attribute_missing'


def test_configured_verification_cannot_be_bypassed_by_similarity_selection(monkeypatch):
    # Contract: dense/similarity candidates in ``chosen`` are not independent
    # evidence. Only an asserted INDEPENDENT_EVIDENCE_KINDS kind may skip, and
    # only for non-exclusivity queries. Do not treat a non-empty list as proof.
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    similar = [object()]
    assert needs_verification('抖音链接应该怎么处理', similar)
    assert needs_verification('抖音链接应该怎么处理', similar, independent_evidence=())
    assert not needs_verification(
        '抖音链接应该怎么处理', similar, independent_evidence='identity_lookup')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '0')
    assert not needs_verification('抖音链接应该怎么处理', similar)
