"""Original-query split identity uses the existing authority namespace."""
from copy import deepcopy
import pytest

from eimemory.evaluation.semantic_recall import validate_dataset


def case(case_id='a', split='development', intent_group='intent-a', **updates):
    return {'case_id':case_id, 'query':'Original Question', 'split':split,
            'intent_group':intent_group, 'scope':{'user_id':'owner'},
            'source_id':'private', 'expected_groups':[['r']], **updates}


def validate(*cases):
    return validate_dataset({'schema':'semantic_recall_cases.v1', 'cases':list(cases)})


def test_renamed_intent_and_case_cannot_move_query_across_splits():
    with pytest.raises(ValueError, match='semantic_query_split_leakage'):
        validate(case(), case('b', 'holdout', 'renamed-intent'))


def test_existing_intent_error_and_same_split_behavior_are_preserved():
    with pytest.raises(ValueError, match='semantic_intent_split_leakage'):
        validate(case(), case('b', 'holdout', 'intent-a'))
    assert len(validate(case(), case('b', 'development', 'renamed-intent'))) == 2


@pytest.mark.parametrize('updates', [
    {'scope':{'tenant_id':'default', 'agent_id':'', 'workspace_id':'', 'user_id':'owner'}},
    {'source_id':'PRIVATE'},
    {'source_id':'ｐｒｉｖａｔｅ'},
    {'task_context':{'goal':'different task context'}},
    {'expected_groups':[['different-ref']]},
])
def test_existing_scope_source_canonicalization_defines_identity(updates):
    with pytest.raises(ValueError, match='semantic_query_split_leakage'):
        validate(case(), case('b', 'holdout', 'renamed-intent', **deepcopy(updates)))


@pytest.mark.parametrize('updates', [
    {'query':'original question'}, {'query':'Original Question '},
    {'scope':{'user_id':'another-owner'}}, {'source_id':'another-source'},
])
def test_query_text_is_not_normalized_and_distinct_namespaces_remain_distinct(updates):
    assert len(validate(case(), case('b', 'holdout', 'renamed-intent', **deepcopy(updates)))) == 2


def test_inherited_scope_and_source_are_applied_before_identity():
    first = case()
    second = case('b', 'holdout', 'renamed-intent')
    for value in (first, second):
        del value['scope']
        del value['source_id']
    with pytest.raises(ValueError, match='semantic_query_split_leakage'):
        validate_dataset({'schema':'semantic_recall_cases.v1', 'scope':{'user_id':'owner'},
                          'source_id':'private', 'cases':[first, second]})
