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


def test_compact_assistance_keeps_proof_digests_without_quotes():
    digest = 'ab' * 32
    result = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'status': 'evidence_found', 'caller_assistance': {
            'status': 'evidence_found', 'outcome': 'supported', 'calls': 1, 'candidate_count': 1,
            'proofs': [
                {'record_id': 'memory-1', 'quote_digest': digest, 'span_start': 3, 'span_end': 7,
                 'quote': 'SECRET'},
                {'quote': 'SECRET'},
            ]}}})
    assert result['caller_assistance']['proofs'] == [{
        'record_id': 'memory-1', 'quote_digest': digest, 'span_start': 3, 'span_end': 7}]
    assert 'SECRET' not in str(result)


def test_compact_assistance_bounds_counts_and_preserves_no_evidence():
    result = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'status': 'no_evidence', 'caller_assistance': {'status': 'no_evidence',
        'outcome': 'no_support', 'calls': -5, 'candidate_count': 10**12}}})
    assert result['admission_status'] == 'no_evidence'
    assert result['caller_assistance'] == {'status': 'no_evidence', 'outcome': 'no_support',
                                           'calls': 0, 'candidate_count': 1000000}


def test_post_selection_projection_ignores_bundle_claims():
    from eimemory.retrieval.stage_diagnostics import retrieval_stage_diagnostics
    result = retrieval_stage_diagnostics({'post_selection': {
        'status': 'SECRET', 'selected_unique_count': 999, 'detail': 'SECRET'}})
    assert result['post_selection'] == {'status': 'unknown'}


def test_post_selection_projection_whitelists_internal_counts():
    from eimemory.retrieval.stage_diagnostics import retrieval_stage_diagnostics
    result = retrieval_stage_diagnostics({}, post_selection={
        'selected_unique_count': 1, 'authorization_input_count': True,
        'authorization_filtered_count': 'SECRET', 'render_empty_count': -1,
        'render_input_count': 10**12, 'session_deduped_count': float('inf'),
        'detail': 'SECRET', 'record_id': 'SECRET', 'credential': 'SECRET',
        'SECRET': 1})
    assert result['post_selection'] == {
        'selected_unique_count': 1, 'authorization_input_count': 'unknown',
        'authorization_filtered_count': 'unknown', 'render_empty_count': 'unknown',
        'render_input_count': 'unknown', 'session_deduped_count': 'unknown'}
    assert 'SECRET' not in str(result)
