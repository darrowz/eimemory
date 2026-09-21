import pytest
from eimemory.retrieval.diagnostics import compact_recall_diagnostics


@pytest.mark.parametrize('reason', [
    'caller_verification_disabled', 'caller_verification_unavailable',
    'caller_model_unavailable', 'caller_verification_failed',
    'caller_model_identity_changed', 'assistance_budget_exhausted',
    'assistance_deadline_exceeded', 'authority_or_deadline_changed',
])
def test_compact_assistance_keeps_only_safe_fields(reason):
    result = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'status': 'unavailable', 'caller_assistance': {
            'status': 'unavailable', 'outcome': 'unavailable', 'reason': reason,
            'calls': 1, 'candidate_count': 8, 'command': 'SECRET',
            'error': 'SECRET', 'query': 'SECRET', 'proofs': [{'quote': 'SECRET'}],
        }}})
    assert result['caller_assistance'] == {
        'status': 'unavailable', 'outcome': 'unavailable', 'reason': reason,
        'calls': 1, 'candidate_count': 8}
    assert 'SECRET' not in str(result)


@pytest.mark.parametrize('value', [None, [], 'SECRET', {'status': 'SECRET',
    'outcome': 'SECRET', 'reason': 'SECRET', 'calls': True, 'candidate_count': float('inf')}])
def test_compact_assistance_malformed_input_does_not_leak(value):
    result = compact_recall_diagnostics({'engine_diagnostics': {},
        'relevance_selector': {'caller_assistance': value}})
    assert 'SECRET' not in str(result)
    if isinstance(value, dict):
        assert result['caller_assistance'] == {'calls': 0, 'candidate_count': 0}
    else:
        assert 'caller_assistance' not in result


def test_compact_assistance_bounds_counts_and_preserves_no_evidence():
    result = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'status': 'no_evidence', 'caller_assistance': {'status': 'no_evidence',
        'outcome': 'no_support', 'calls': -5, 'candidate_count': 10**12}}})
    assert result['admission_status'] == 'no_evidence'
    assert result['caller_assistance'] == {'status': 'no_evidence', 'outcome': 'no_support',
                                           'calls': 0, 'candidate_count': 1000000}
