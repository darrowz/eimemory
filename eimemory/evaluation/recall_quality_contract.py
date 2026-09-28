"""Quality evidence accounting, independent of Runtime and release authority.

A successful diagnostic execution is not quality acceptance. Safety failures
are evaluated BEFORE evidence sufficiency, including on undersized data sets.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

RATIO_METRICS = frozenset({
    'hit_at_1', 'hit_at_5', 'p_at_3', 'mrr', 'noise_rate', 'padding_rate',
    'false_recall_rate', 'forbidden_hit_rate', 'outcome_pollution_rate',
    'reflection_pollution_rate', 'audit_pollution_rate', 'incident_pollution_rate',
    'evolution_pollution_rate', 'stale_rule_pollution_rate', 'selected_record_pollution_rate',
})
RANK_METRICS = frozenset({'hit_at_1', 'hit_at_5', 'p_at_3', 'mrr', 'noise_rate'})
LEAKAGE_METRICS = ('cross_channel_leakage_count', 'source_filter_leakage_count')


def _number(value: Any, *, ratio: bool = False) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return isfinite(value) and value >= 0 and (not ratio or value <= 1)
    except OverflowError:
        return False


def evaluate_quality_report(
    report: Mapping[str, Any], *, limits: Mapping[str, float],
    minimum_metrics: set[str], minimum_samples: int,
    judged_contract: str, required_roles: frozenset[str],
) -> dict[str, Any]:
    """Validate values and execution before deciding 'failed' vs 'insufficient'.

    No missing metric defaults to a passing zero. Metric diagnostics contain
    only bounded numeric values/codes, never private query or answer text.
    """
    if not isinstance(report, Mapping):
        raise ValueError('recall_quality_report_must_be_object')
    if type(minimum_samples) is not int or minimum_samples < 1:
        raise ValueError('recall_quality_minimum_invalid')
    limits = dict(limits)
    if any(not _number(v, ratio=k in RATIO_METRICS) for k, v in limits.items()):
        raise ValueError('recall_quality_threshold_invalid')
    count = report.get('sample_count')
    count_valid = type(count) is int and count >= 0
    contract = str(report.get('evaluation_contract') or 'legacy.v1')
    smoke = contract == 'known_item_smoke.v1'
    judged = contract == judged_contract
    raw_roles = report.get('label_roles', [])
    roles_valid = isinstance(raw_roles, (list, tuple, set, frozenset)) and all(
        isinstance(v, str) for v in raw_roles
    )
    roles = set(raw_roles) if roles_valid else set()
    trust = report.get('label_trust')
    trusted = isinstance(trust, str) and trust in {'operator_judged', 'machine_judged'}
    missing_roles = sorted(required_roles - roles) if judged else []
    short = not count_valid or count < minimum_samples
    insufficient = smoke or short or (judged and (not trusted or missing_roles))
    invalid: list[str] = []
    missing: list[str] = []
    blocking: dict[str, dict[str, Any]] = {}
    if not count_valid:
        invalid.append('sample_count')
        blocking['sample_count'] = {'reason': 'nonnegative_integer_required'}
    if judged and not roles_valid:
        invalid.append('label_roles')
        blocking['label_roles'] = {'reason': 'string_array_required'}
    errors = report.get('errors', [])
    seed_errors = report.get('seed_error_count', 0)
    execution_failed = bool(report.get('error')) or not isinstance(errors, list) or bool(errors)
    execution_failed = execution_failed or ('ok' in report and report['ok'] is not True)
    execution_failed = execution_failed or ('execution_ok' in report and report['execution_ok'] is not True)
    execution_failed = execution_failed or type(seed_errors) is not int or seed_errors != 0
    samples = report.get('samples')
    if samples is not None:
        if not isinstance(samples, list) or (count_valid and len(samples) != count):
            execution_failed = True
        elif any(not isinstance(row, dict) or row.get('error') for row in samples):
            execution_failed = True
    if execution_failed:
        blocking['execution'] = {'reason': 'recall_execution_error'}
    for metric in LEAKAGE_METRICS:
        value = report.get(metric)
        if value is None:
            missing.append(metric)
        elif type(value) is not int or value < 0:
            invalid.append(metric)
            blocking[metric] = {'reason': 'nonnegative_integer_required'}
        elif value:
            blocking[metric] = {'actual': value, 'threshold': 0, 'operator': '=='}
    for metric, threshold in limits.items():
        value = report.get(metric)
        if value is None:
            missing.append(metric)
            continue
        if not _number(value, ratio=metric in RATIO_METRICS):
            invalid.append(metric)
            blocking[metric] = {'reason': 'finite_nonnegative_number_required'}
            continue
        # Rank diagnostics on unlabeled/undersized observations are not judged
        # quality. Isolation, contamination and resource ceilings always apply.
        if insufficient and metric in RANK_METRICS:
            continue
        low = metric in minimum_metrics
        if (low and value < threshold) or (not low and value > threshold):
            blocking[metric] = {
                'actual': value, 'threshold': threshold, 'operator': '>=' if low else '<=',
            }
    insufficient = insufficient or bool(missing)
    status = 'failed' if blocking else 'insufficient' if insufficient else 'sufficient'
    evidence = {
        'schema': 'recall_quality_evidence.v1',
        'status': status,
        'evaluation_contract': contract,
        'sample_count': count if count_valid else None,
        'minimum_sample_count': minimum_samples,
        'missing_sample_count': max(0, minimum_samples - count) if count_valid else None,
        'required_roles': sorted(required_roles) if judged else [],
        'missing_roles': missing_roles,
        'label_trust_accepted': trusted if judged else False,
        'missing_metrics': sorted(set(missing)),
        'invalid_metrics': sorted(set(invalid)),
        'execution_ok': not execution_failed and count_valid,
        # This module validates diagnostics, not authenticated label provenance.
        'release_authority_verified': False,
    }
    return {
        'ok': status == 'sufficient',
        'policy': 'production_recall_pollution_gate',
        'blocked_reason': 'recall_quality_gate_failed' if blocking else
                          'recall_quality_evidence_incomplete' if insufficient else '',
        'evidence_status': status,
        'thresholds': limits,
        'blocking_metrics': blocking,
        'unassessed_metrics': sorted(RANK_METRICS) if insufficient else [],
        'vacuous': insufficient,
        'label_roles': sorted(roles),
        'required_roles': sorted(required_roles) if judged else [],
        'recall_quality_evidence': evidence,
    }
