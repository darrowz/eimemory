"""Bundle status is an independent prerequisite for semantic acceptance."""
from copy import deepcopy
import pytest

from eimemory.evaluation.semantic_recall import (
    _semantic_bundle_path_verified, score_case, semantic_path_verified,
)


def identity():
    return {'relevance_admission':{'enabled':True}, 'candidate_source':{'postgres':{
        'state':'available', 'index_verified':True, 'query_valid':True, 'bypass_reason':''}}}


@pytest.mark.parametrize('status', ['evidence_found', 'no_evidence', 'identity_lookup'])
def test_explicit_healthy_bundle_statuses_keep_existing_path_requirement(status):
    assert _semantic_bundle_path_verified(identity(), {
        'retrieval_status':status, 'relevance_selector':{'status':status}})


@pytest.mark.parametrize('status', ['degraded', 'unavailable', 'unknown', '', None, False, [], {}])
def test_nonhealthy_or_missing_status_is_not_replaced_by_good_selector(status):
    explanation = {'retrieval_status':status, 'relevance_selector':{'status':'evidence_found'}}
    assert not _semantic_bundle_path_verified(identity(), explanation)
    assert not _semantic_bundle_path_verified(identity(), {'relevance_selector':{'status':'evidence_found'}})


def test_degraded_empty_result_cannot_certify_a_negative():
    path = _semantic_bundle_path_verified(identity(), {
        'retrieval_status':'degraded', 'relevance_selector':{'status':'no_evidence'}})
    result = score_case([], [], unavailable=not path)
    assert result['unavailable'] and not result['passed']


def test_helper_interface_and_existing_identity_rejections_are_unchanged():
    selector = {'status':'no_evidence'}
    assert semantic_path_verified(identity(), selector)
    assert not semantic_path_verified(identity(), {})
    explanation = {'retrieval_status':'no_evidence', 'relevance_selector':selector}
    for key, value in [('state','bypassed'), ('query_valid',False), ('index_verified',False),
                       ('bypass_reason','authority_changed')]:
        changed = deepcopy(identity())
        changed['candidate_source']['postgres'][key] = value
        assert not _semantic_bundle_path_verified(changed, explanation)
    changed = identity()
    changed['relevance_admission']['enabled'] = False
    assert not _semantic_bundle_path_verified(changed, explanation)
    assert not _semantic_bundle_path_verified(identity(), {
        'retrieval_status':'evidence_found', 'relevance_selector':{'status':'degraded'}})

@pytest.mark.parametrize('status', ['evidence_found', 'degraded', 'unavailable'])
@pytest.mark.parametrize('source', ['private', 'PRIVATE', 'ｐｒｉｖａｔｅ'])
def test_evaluator_uses_canonical_authority_and_final_bundle_status(status, source):
    from types import SimpleNamespace
    from eimemory.evaluation.semantic_recall import evaluate_semantic_recall
    from eimemory.models.records import ScopeRef
    calls = []
    item = SimpleNamespace(record_id='r', scope=ScopeRef(user_id='owner'),
                           source_id='private', status='active')
    def exact(ref, *, scope, source_id):
        calls.append(('label', source_id))
        return item if ref == 'r' and scope == item.scope and source_id == 'private' else None
    def recall(**kwargs):
        calls.append(('recall', kwargs['task_context']['source_ids']))
        return SimpleNamespace(items=[item], explanation={
            'retrieval_status':status, 'relevance_selector':{'status':'evidence_found'}})
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_exact_ref=exact), memory=SimpleNamespace(
        recall=recall, recall_engine=SimpleNamespace(effective_identity=identity)))
    case = {'case_id':'a', 'query':'Original Question', 'split':'development',
            'intent_group':'intent-a', 'scope':{'user_id':'owner'},
            'source_id':source, 'expected_groups':[['r']]}
    report = evaluate_semantic_recall(runtime, {'schema':'semantic_recall_cases.v1','cases':[case]})
    sample = report['samples'][0]
    assert sample['metrics']['unavailable'] is (status != 'evidence_found')
    assert sample['metrics']['passed'] is (status == 'evidence_found')
    assert sample['metrics']['forbidden_hit_count'] == 0
    assert calls == [('label', 'private'), ('recall', ['private'])]
    assert report['passed'] is False  # One case cannot satisfy the release sample floor.
