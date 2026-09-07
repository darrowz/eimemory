from copy import deepcopy

import pytest

from eimemory.evaluation.semantic_recall import score_case, summarize, validate_dataset
from eimemory.evaluation.original_query_recall import _metrics


def test_equivalent_answers_are_alternatives_and_tails_count_as_noise():
    assert score_case(['a'],[['a','b']])['passed']
    assert score_case(['a'],[['a','b']])['group_recall'] == 1
    assert not score_case(['a','noise'],[['a','b']])['passed']
    assert not score_case(['noise','a'],[['a','b']])['passed']
    assert not score_case(['a'],[['a'],['c']])['passed']


def test_empty_unavailable_is_not_a_successful_negative():
    assert score_case([],[])['passed']
    assert not score_case([],[],unavailable=True)['passed']
    assert not score_case(['anything'],[])['passed']
    assert not score_case(['a'],[['a']],boundary_violation=True)['passed']


def test_quality_summary_has_negative_and_actual_returned_precision_gates():
    samples = [{'metrics':score_case(['a'],[['a']]),'latency_ms':10},
               {'metrics':score_case([],[]),'latency_ms':10}]
    assert summarize(samples)['passed']
    samples[1]['metrics'] = score_case(['noise'],[])
    result = summarize(samples)
    assert not result['passed']
    assert result['metrics']['returned_precision'] == .5
    assert result['metrics']['false_recall_rate'] == 1
    assert not summarize(samples[:1])['passed']  # no negative coverage is unknown


def test_original_metrics_do_not_divide_by_zero_or_pad_actual_precision():
    assert _metrics([],[])['false_recall'] is False
    metrics = _metrics(['a'],[{'record_ref':'a'}])
    assert metrics['precision_at_5'] == .2
    assert metrics['returned_precision'] == 1


def test_freeze_rejects_intent_leakage_and_conflicting_labels():
    case = {'case_id':'a','query':'question','split':'development','intent_group':'intent',
        'scope':{'user_id':'owner'},'source_id':'private','expected_groups':[['a']]}
    dataset = {'schema':'semantic_recall_cases.v1','cases':[case]}
    validate_dataset(dataset)
    other = {**deepcopy(case),'case_id':'b','split':'holdout'}
    with pytest.raises(ValueError,match='split_leakage'):
        validate_dataset({**dataset,'cases':[case,other]})
    with pytest.raises(ValueError,match='forbidden_refs_invalid'):
        validate_dataset({**dataset,'cases':[{**case,'forbidden_refs':['a']}]})


def test_thresholds_cannot_be_overridden_by_dataset():
    from eimemory.evaluation.semantic_recall import THRESHOLDS
    assert THRESHOLDS['hit_at_1'] == .90
    assert THRESHOLDS['false_recall_rate'] == .05
    assert THRESHOLDS['latency_ms_p95'] == 3000
