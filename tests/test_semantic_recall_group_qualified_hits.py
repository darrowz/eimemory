"""Group-qualified positive hits retain the existing aggregate tolerances."""
import pytest

from eimemory.evaluation.semantic_recall import (
    THRESHOLDS, release_quality_passed, score_case, summarize, validate_dataset,
)


def test_partial_groups_do_not_qualify_as_positive_hits():
    result = score_case(['a'], [['a'], ['b']])
    assert result['hit_at_1'] is False
    assert result['hit_at_5'] is False
    assert result['group_recall'] == .5
    assert result['returned_relevant_count'] == result['returned_count'] == 1
    assert not result['passed']


def test_alternatives_rank_and_noise_keep_their_existing_meanings():
    alternative = score_case(['b'], [['a', 'b']])
    assert alternative['hit_at_1'] and alternative['hit_at_5'] and alternative['passed']
    complete = score_case(['a', 'b'], [['a'], ['b']])
    assert complete['hit_at_1'] and complete['hit_at_5'] and complete['passed']
    noisy = score_case(['noise', 'a', 'b'], [['a'], ['b']])
    assert not noisy['hit_at_1'] and noisy['hit_at_5'] and not noisy['passed']
    assert noisy['returned_relevant_count'] == 2 and noisy['returned_count'] == 3


def test_group_qualification_uses_deduplicated_top_five_not_just_first_result():
    within_five = score_case(['a', 'n1', 'n2', 'n3', 'b'], [['a'], ['b']])
    assert within_five['hit_at_1'] and within_five['hit_at_5']
    assert within_five['group_recall'] == 1 and not within_five['passed']
    outside_five = score_case(['a', 'n1', 'n2', 'n3', 'n4', 'b'], [['a'], ['b']])
    assert not outside_five['hit_at_1'] and not outside_five['hit_at_5']
    assert outside_five['group_recall'] == .5 and outside_five['returned_count'] == 5
    deduplicated = score_case(['a', 'a', 'n1', 'n2', 'n3', 'b'], [['a'], ['b']])
    assert deduplicated == within_five


@pytest.mark.parametrize('partial_count,expected', [(0, True), (1, True), (2, True), (3, False), (20, False)])
def test_group_qualified_hits_use_existing_aggregate_thresholds(partial_count, expected):
    positives = [score_case(['a'], [['a'], ['b']]) for _ in range(partial_count)]
    positives += [score_case(['a', 'b'], [['a'], ['b']]) for _ in range(20-partial_count)]
    samples = [{'split':'holdout', 'metrics':metric, 'latency_ms':10} for metric in positives]
    samples += [{'split':'holdout', 'metrics':score_case([], []), 'latency_ms':10} for _ in range(20)]
    samples += [{'split':'regression', 'metrics':score_case(['r'], [['r']]), 'latency_ms':10} for _ in range(20)]
    holdout = summarize(samples[:40])
    assert holdout['metrics']['hit_at_1'] == (20-partial_count)/20
    assert holdout['metrics']['hit_at_5'] == (20-partial_count)/20
    assert holdout['metrics']['returned_precision'] == 1
    assert holdout['passed'] is expected
    assert release_quality_passed(samples, {'holdout':holdout}) is expected
    assert THRESHOLDS['hit_at_1'] == THRESHOLDS['hit_at_5'] == .90


def test_no_group_negative_and_missing_declaration_contracts_are_unchanged():
    negative = score_case([], [])
    assert negative['hit_at_1'] is None and negative['hit_at_5'] is None
    assert negative['false_recall'] is False and negative['passed']
    assert not score_case(['noise'], [])['passed']
    assert not score_case([], [], unavailable=True)['passed']
    case = {'case_id':'a', 'query':'question', 'split':'development', 'intent_group':'intent',
            'scope':{'user_id':'owner'}, 'source_id':'private'}
    with pytest.raises(ValueError, match='semantic_expected_groups_invalid'):
        validate_dataset({'schema':'semantic_recall_cases.v1', 'cases':[case]})
