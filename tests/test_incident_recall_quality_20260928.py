from copy import deepcopy
import math
import pytest

from eimemory.evaluation.production_recall import (
    evaluate_production_recall_quality_gate as evaluate,
    RECALL_QUALITY_GATE_THRESHOLDS,
)


def valid_report():
    metrics = {k: (1.0 if k in {'hit_at_1', 'hit_at_5', 'p_at_3', 'mrr'} else 0.0)
               for k in RECALL_QUALITY_GATE_THRESHOLDS}
    return dict(metrics, sample_count=10, errors=[], seed_error_count=0,
                cross_channel_leakage_count=0, source_filter_leakage_count=0,
                evaluation_contract='judged_relevance.v1',
                label_roles=['positive', 'rewrite', 'no_answer'], label_trust='operator_judged')


def test_valid_positive_control():
    assert evaluate(valid_report())['ok'] is True


@pytest.mark.parametrize('field', ['cross_channel_leakage_count', 'source_filter_leakage_count'])
@pytest.mark.parametrize('count', [0, 3, 10])
def test_missing_roles_never_hide_leakage(field, count):
    r = valid_report(); r.update(sample_count=count, label_roles=['positive']); r[field] = 1
    gate = evaluate(r)
    assert gate['ok'] is False
    assert gate['blocked_reason'] == 'recall_quality_gate_failed'
    assert field in gate['blocking_metrics']


@pytest.mark.parametrize('field', ['false_recall_rate', 'payload_bytes_top_5', 'latency_ms_p95'])
def test_insufficient_sample_safety_still_checked(field):
    r = valid_report(); r.update(sample_count=1, label_roles=[])
    r[field] = 1.0 if field == 'false_recall_rate' else 100000
    assert field in evaluate(r)['blocking_metrics']


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, True, 'nan', -1, 1.01])
def test_nonfinite_or_invalid_metric_never_passes(value):
    r = valid_report(); r['hit_at_1'] = value
    assert evaluate(r)['ok'] is False


@pytest.mark.parametrize('value', [True, False, 10.1, '10', -1, math.inf])
def test_sample_count_not_coerced(value):
    r = valid_report(); r['sample_count'] = value
    gate = evaluate(r)
    assert gate['ok'] is False
    assert 'sample_count' in gate['blocking_metrics']


@pytest.mark.parametrize('count', [0, 1, 9])
def test_undersized_legacy_cannot_certify_quality(count):
    r = valid_report(); r.update(evaluation_contract='legacy.v1', sample_count=count)
    gate = evaluate(r)
    assert gate['ok'] is False
    assert gate['blocked_reason'] == 'recall_quality_evidence_incomplete'


@pytest.mark.parametrize('field', ['hit_at_1', 'false_recall_rate', 'cross_channel_leakage_count'])
def test_missing_metrics_are_explicit_not_passing_zero(field):
    r = valid_report(); r.pop(field)
    gate = evaluate(r)
    assert gate['ok'] is False
    assert field in gate['recall_quality_evidence']['missing_metrics']


def test_known_item_smoke_never_becomes_judged_relevance():
    r = valid_report(); r['evaluation_contract'] = 'known_item_smoke.v1'
    gate = evaluate(r)
    assert not gate['ok']
    assert gate['recall_quality_evidence']['release_authority_verified'] is False


@pytest.mark.parametrize('error', [{'errors': ['seed failed']}, {'seed_error_count': 1},
                                  {'samples': [{'error': 'failed'}] * 10}])
def test_execution_errors_cannot_become_waits(error):
    r = valid_report(); r.update(label_roles=[], **error)
    gate = evaluate(r)
    assert gate['blocked_reason'] == 'recall_quality_gate_failed'
    assert gate['recall_quality_evidence']['execution_ok'] is False


def test_bad_threshold_is_rejected_without_coercion():
    with pytest.raises(ValueError):
        evaluate(valid_report(), thresholds={'hit_at_1': True})
