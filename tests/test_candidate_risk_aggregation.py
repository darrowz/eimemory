"""High-risk aggregation preserves the existing per-case risk contract."""
from __future__ import annotations

import copy

import pytest

from eimemory.governance.learning import candidate_search as cs
from eimemory.governance.learning.outcome_replay import build_replay_case_from_outcome
from eimemory.governance.learning.rule_evolution import _rule_candidates_from_outcome_traces
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.experience.outcome import build_outcome_trace_record

MISSING_HIGH = ('privacy', 'device', 'account', 'ha', 'L2-device', 'L3:account', 'L4/device')
CANONICAL_HIGH = ('high', 'unsafe', 'l4', 'l3', 'l2')


def replay_cases(levels):
    return [dict(risk_level=risk, task_type='synthetic.inspect', primary_label='missing_tool_call', signals=['operator_gap'], expected_text=['Inspect synthetic input'], source_outcome_trace_id=f'synthetic-{i}') for i, risk in enumerate(levels)]


def scored(cases):
    return cs.score_proxy_candidates(cs.generate_candidate_policies(cases), cases)['top_candidate']


def high_contract_mismatches(candidate):
    checks = {
        'aggregated_high_risk': cs._is_high_risk(candidate['risk_level']),
        'initial_status_candidate': candidate['initial_status'] == 'candidate',
        'allow_auto_promote_false': candidate['promotion_gate']['allow_auto_promote'] is False,
        'requires_review_true': candidate['promotion_gate']['requires_review'] is True,
        'high_risk_penalty_applied': candidate['proxy_eval']['score'] == 22.0,
        'audit_risk_high': cs._is_high_risk(candidate['audit_meta']['risk_level']),
    }
    return [name for name, passed in checks.items() if not passed]


@pytest.mark.parametrize('risk', MISSING_HIGH)
@pytest.mark.parametrize('high_first', (False, True))
def test_existing_high_risk_classification_survives_aggregation(risk, high_first):
    assert cs._is_high_risk(risk)
    levels = [risk, 'low'] if high_first else ['low', risk]
    candidate = scored(replay_cases(levels))
    assert not high_contract_mismatches(candidate), {'levels': levels, 'mismatches': high_contract_mismatches(candidate), 'candidate': candidate}


@pytest.mark.parametrize('risk', CANONICAL_HIGH)
@pytest.mark.parametrize('high_first', (False, True))
def test_canonical_high_markers_still_conservative(risk, high_first):
    levels = [risk, 'low'] if high_first else ['low', risk]
    assert not high_contract_mismatches(scored(replay_cases(levels)))


@pytest.mark.parametrize('risk', ('low', 'safe', 'software', 'l0', 'l1'))
def test_all_low_cases_retain_existing_contract(risk):
    candidate = scored(replay_cases([risk, risk]))
    assert cs._is_low_risk(candidate['risk_level'])
    assert candidate['initial_status'] == 'shadow'
    assert candidate['promotion_gate']['allow_auto_promote'] is True
    assert candidate['proxy_eval']['score'] == 26.0


def test_primary_label_is_independent_high_risk_guard():
    cases = replay_cases(['low', 'low'])
    for case in cases:
        case['primary_label'] = 'unsafe_or_high_risk'
    candidate = scored(cases)
    assert candidate['initial_status'] == 'candidate'
    assert candidate['promotion_gate']['allow_auto_promote'] is False
    assert candidate['promotion_gate']['requires_review'] is True


def test_no_input_mutation():
    cases = replay_cases(['low', 'privacy'])
    original = copy.deepcopy(cases)
    scored(cases)
    assert cases == original


def stored_shape_records(risks):
    records = []
    for index, risk in enumerate(risks):
        trace = RecordEnvelope.create(
            kind='reflection', title='Synthetic trace', summary='Synthetic failure',
            scope=ScopeRef.from_dict({'workspace_id': 'synthetic-risk-audit'}),
            content={'payload': {'query': 'Inspect synthetic input'}, 'diagnosis': {'expected_text': ['Inspect synthetic input'], 'signals': ['operator_gap']}},
            meta={'report_type': 'outcome_trace', 'schema_version': 'outcome_trace.v1', 'task_type': 'synthetic.inspect', 'primary_label': 'missing_tool_call', 'diagnosis_signals': ['operator_gap'], 'risk_level': risk},
        )
        ts = f'2026-10-02T12:00:0{index}+00:00'
        trace.time = TimeRef(ts, ts, ts)
        records.append(trace)
    return records


@pytest.mark.parametrize('risk', ('privacy', 'device', 'account', 'ha'))
def test_persisted_shape_to_replay_to_candidate_chain(risk):
    records = stored_shape_records(['low', risk])
    cases = [build_replay_case_from_outcome(record) for record in records]
    assert [case['risk_level'] for case in cases] == ['low', risk]
    candidates = _rule_candidates_from_outcome_traces(outcome_traces=list(reversed(records)), rules=[])
    assert len(candidates) == 2
    assert all(cs._is_high_risk(candidate['risk_level']) for candidate in candidates), [(c['source_type'], c['risk_level'], c['promotion_gate'], c['proxy_eval']) for c in candidates]


def built_records(risks):
    return [build_outcome_trace_record({
        'trace_id': f'synthetic-risk-trace-{index}',
        'recorded_at': f'2026-10-02T12:00:0{index}+00:00',
        'task_type': 'synthetic.inspect', 'query': 'Inspect synthetic input',
        'outcome': {'status': 'failure', 'rehearsal': True},
        'actions': [], 'expected_tool': 'synthetic_inspect',
        'verifier': {'passed': False}, 'risk_level': risk,
        'expected_text': ['Inspect synthetic input'],
    }, scope={'workspace_id': 'synthetic-risk-audit'}).record for index, risk in enumerate(risks)]


def test_canonical_builder_ha_reaches_bug_without_forging_diagnosis():
    records = built_records(['low', 'ha'])
    cases = [build_replay_case_from_outcome(record) for record in records]
    assert [case['primary_label'] for case in cases] == ['missing_tool_call', 'missing_tool_call']
    assert [case['risk_level'] for case in cases] == ['low', 'ha']
    candidates = _rule_candidates_from_outcome_traces(outcome_traces=records, rules=[])
    assert len(candidates) == 1
    assert candidates[0]['risk_level'] == 'ha', candidates[0]


@pytest.mark.parametrize('risk', ('privacy', 'device', 'account', 'L2-device', 'L3:account', 'L4/device'))
def test_canonical_builder_normalizes_other_high_markers(risk):
    record = built_records([risk])[0]
    replay = build_replay_case_from_outcome(record)
    assert replay['primary_label'] == 'unsafe_or_high_risk'
    assert replay['risk_level'] == 'high'


@pytest.mark.parametrize('levels, expected', (
    (['privacy', 'l2', 'unsafe', 'high'], 'high'),
    (['device', 'l2', 'unsafe'], 'unsafe'),
    (['account', 'l2', 'l3', 'l4'], 'l4'),
    (['ha', 'l2', 'l3'], 'l3'),
    (['privacy', 'l2'], 'l2'),
))
def test_existing_canonical_marker_priority_is_preserved(levels, expected):
    assert cs._max_risk_level(levels) == expected
    assert cs._max_risk_level(list(reversed(levels))) == expected


@pytest.mark.parametrize('risk', MISSING_HIGH)
def test_non_high_values_cannot_hide_existing_high_risk(risk):
    assert cs._is_high_risk(cs._max_risk_level(['medium', 'unrecognized', risk]))


def test_whitespace_and_case_normalization_still_apply():
    assert cs._max_risk_level([' LOW ', ' PRIVACY ']) == 'privacy'


def test_absent_risk_retains_review_default():
    assert cs._max_risk_level([]) == 'medium'
    assert cs._max_risk_level(['', '  ']) == 'medium'
