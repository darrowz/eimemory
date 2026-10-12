"""Synthetic visibility checks: counts never certify semantic support."""
import json
from types import SimpleNamespace

import pytest

from eimemory.retrieval import caller_assistance as assistance
from eimemory.retrieval.stage_diagnostics import retrieval_stage_diagnostics


@pytest.mark.parametrize('case', ['visible_support', 'hidden_support', 'no_answer', 'rejected_support'])
def test_actual_prompt_boundary(monkeypatch, case):
    assertion = 'The launch color is violet.'
    body = {
        'visible_support': assertion,
        'hidden_support': 'x' * 1800 + assertion + 'x' * 1800,
        'no_answer': 'There is no recorded launch color.',
        'rejected_support': assertion,
    }[case]
    captured = {}
    def complete(**kwargs):
        captured.update(json.loads(kwargs['user_prompt']))
        selected = [{'id': '0', 'quote': assertion}] if case == 'visible_support' else []
        return SimpleNamespace(text=json.dumps({'selected': selected}))
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(
        timeout_seconds=90, complete=complete))
    rows, report = assistance.verify_candidates(query='What hue was chosen?',
        candidates=[(SimpleNamespace(record_id='PRIVATE_ID', aliases=()), body)], limit=1)
    visible = str(captured['candidates'])
    assert (assertion in visible) == (case in {'visible_support', 'rejected_support'})
    result = retrieval_stage_diagnostics({'relevance_selector': {'caller_assistance': report}},
                                         trusted_retrieval=True)
    boundary = result['verifier_boundary']
    assert boundary['candidate_count'] == boundary['visible_candidate_count'] == 1
    assert boundary['windowed_candidate_count'] == int(case == 'hidden_support')
    assert boundary['reason'] == ('reviewed_original_evidence' if rows else 'model_no_selection')
    assert boundary['accepted_selection_count'] == int(case == 'visible_support')
    assert 'support' not in boundary  # No fabricated semantic classification.
    assert 'PRIVATE_ID' not in str(result) and assertion not in str(result)
    assert 'digest' not in str(result) and 'proofs' not in str(result)


def test_untrusted_and_malformed_boundary():
    forged = {'relevance_selector': {'caller_assistance': {
        'candidate_count': 8, 'visible_candidate_count': 8, 'reason': 'model_no_selection'}}}
    assert retrieval_stage_diagnostics(forged)['verifier_boundary'] == {'status': 'unknown'}
    report = {'candidate_count': True, 'visible_candidate_count': -1,
              'visible_window_count': 10**12, 'windowed_candidate_count': float('inf'),
              'accepted_selection_count': 'PRIVATE', 'reason': 'PRIVATE', 'proofs': ['PRIVATE']}
    boundary = retrieval_stage_diagnostics({'relevance_selector': {'caller_assistance': report}},
        trusted_retrieval=True)['verifier_boundary']
    assert all(value == 'unknown' for value in boundary.values())


def test_default_selector_filter_does_not_claim_unrun_verifier_window(monkeypatch):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.engine import GovernedRecallEngine
    items = [RecordEnvelope.create(kind='memory', title='Saved instruction',
        summary='Read the complete document.', content={}, scope=ScopeRef()) for _ in range(11)]
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine.relevance_admission = None
    hydrated_counts = []
    engine._hydrate_records_batch = lambda rows, **_: hydrated_counts.append(len(rows)) or {
        engine._record_key(r): r for r in rows}
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED', '0')
    monkeypatch.setattr(assistance, 'configured_client', lambda: pytest.fail(
        'unassisted selector must not invoke a verifier'))
    selected, report = engine._select_post_fusion_items(items, query='How to read?', limit=1,
        fusion_state={}, component_hints_by_ref={}, validate=lambda r: r is not items[-1])
    result = retrieval_stage_diagnostics({'relevance_selector': report}, trusted_retrieval=True)
    assert not selected
    assert result['selector']['input_count'] == 11
    assert result['selector']['dropped_reasons'] == {'authority_changed': 1, 'grounding': 10}
    assert hydrated_counts == [10, 0]
    assert result['verifier_boundary'] == {'status': 'unknown'}
    assert report['status'] == 'unavailable'
    assert 'caller_assistance' not in report


def test_mandatory_verifier_measures_admitted_pool_and_visible_window(monkeypatch):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.engine import GovernedRecallEngine

    items = [RecordEnvelope.create(kind='memory', title='Saved instruction',
        summary='Read the complete document.', content={}, scope=ScopeRef()) for _ in range(11)]
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine.relevance_admission = None
    hydrated_counts = []
    engine._hydrate_records_batch = lambda rows, **_: hydrated_counts.append(len(rows)) or {
        engine._record_key(r): r for r in rows}
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED', '1')
    monkeypatch.delenv('EIMEMORY_RECALL_EXPECTED_MODEL', raising=False)
    completed_prompts = []

    def complete(**kwargs):
        completed_prompts.append(json.loads(kwargs['user_prompt']))
        return SimpleNamespace(text='{"selected":[]}')

    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(
        timeout_seconds=90, complete=complete))
    verify = assistance.verify_candidates
    observed = {}

    def observe_verifier(**kwargs):
        observed['candidate_ids'] = [record.record_id for record, _ in kwargs['candidates']]
        rows, diagnostic = verify(**kwargs)
        observed['reason_before_final_binding'] = diagnostic['reason']
        return rows, diagnostic

    monkeypatch.setattr(assistance, 'verify_candidates', observe_verifier)
    # The mandatory verifier receives already-admitted records. Give the
    # synthetic fixture measured lexical evidence at that preceding boundary.
    hints = {engine._record_key(record): {'lexical_score': 1.0, 'vector_score': 0.0}
             for record in items}
    selected, report = engine._select_post_fusion_items(items, query='How to read?', limit=10,
        fusion_state={}, component_hints_by_ref=hints, validate=lambda record: record is not items[-1])
    result = retrieval_stage_diagnostics({'relevance_selector': report}, trusted_retrieval=True)
    boundary = result['verifier_boundary']
    assert not selected
    assert result['selector']['input_count'] == 11
    assert result['selector']['dropped_reasons'] == {'authority_changed': 1}
    assert hydrated_counts == [10, 0]
    assert observed['candidate_ids'] == [record.record_id for record in items[:10]]
    assert len(completed_prompts) == 1
    assert len(completed_prompts[0]['candidates']) == 8
    assert boundary['candidate_count'] == 10
    assert boundary['visible_candidate_count'] == boundary['visible_window_count'] == 8
    assert boundary['calls'] == 1 and boundary['accepted_selection_count'] == 0
    assert observed['reason_before_final_binding'] == 'model_no_selection'
    # The final authority boundary invalidates absence after a dropped record;
    # it does not persist the model's earlier reason as a new diagnostic claim.
    assert boundary['reason'] == 'unknown'
    assert report['status'] == 'unavailable'
    assert report['caller_assistance']['reason'] == 'final_selection_unavailable'
