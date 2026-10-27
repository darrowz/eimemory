"""Completed empty retrieval is a negative outcome, not a retryable outage."""
import pytest

from eimemory.evaluation import semantic_gap_repair as repair
from eimemory.models.records import RecallBundle
from test_semantic_gap_repair import incident
from test_semantic_gap_repair_flow import replay_case


def empty_recall(**kwargs):
    return RecallBundle(items=[], rules=[], reflections=[], confidence=0,
        next_action_hint='', explanation={'retrieval_status': 'no_evidence',
        'relevance_selector': {'collection_complete': True, 'status': 'no_evidence'},
        'engine_diagnostics': {'source_budget_exhausted': 0, 'drops': {}}})


def test_completed_empty_case_does_not_grow_retry_chain(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    calls = []
    def recall(**kwargs):
        calls.append(kwargs)
        return empty_recall(**kwargs)
    monkeypatch.setattr(runtime.memory, 'recall', recall)
    before = runtime.store.get_by_id(gap.record_id).to_dict()
    reports = [repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=8)
               for _ in range(5)]
    assert len(calls) == 1
    assert not judges
    for report, findings in reports:
        assert findings == [] and report['provider_calls'] == 0
        assert report['unverifiable_count'] == 0
        assert report['unresolved_count'] >= 1
        assert set(report['reason_counts']) == {'semantic_repair_no_evidence'}
    assert runtime.store.get_by_id(gap.record_id).to_dict() == before


def test_complete_empty_still_cannot_authorize_review(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    monkeypatch.setattr(runtime.memory, 'recall', empty_recall)
    packet = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    with pytest.raises(ValueError, match='^semantic_repair_no_evidence$'):
        repair.review_semantic_gap_replay(runtime, packet_ref=packet['packet_ref'], scope=scope)
    assert not judges


@pytest.mark.parametrize('change', ['incomplete', 'budget', 'unknown', 'mismatch'])
def test_empty_with_missing_or_failed_stage_remains_retryable(replay_case, monkeypatch, change):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    def recall(**kwargs):
        b = empty_recall(**kwargs)
        if change == 'incomplete': b.explanation['relevance_selector']['collection_complete'] = False
        if change == 'budget': b.explanation['engine_diagnostics']['source_budget_exhausted'] = 1
        if change == 'unknown': b.explanation['relevance_selector']['collection_complete'] = None
        if change == 'mismatch': b.explanation['retrieval_status'] = 'degraded'
        return b
    monkeypatch.setattr(runtime.memory, 'recall', recall)
    packet = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    with pytest.raises(ValueError, match='^semantic_repair_retrieval_not_complete$'):
        repair.review_semantic_gap_replay(runtime, packet_ref=packet['packet_ref'], scope=scope)
    assert not judges


def test_explicit_attempt_can_replay_repaired_corpus_without_rewriting_empty_packet(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    normal = runtime.memory.recall
    monkeypatch.setattr(runtime.memory, 'recall', empty_recall)
    first = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    old = runtime.store.get_by_id(first['packet_ref']).to_dict()
    monkeypatch.setattr(runtime.memory, 'recall', normal)
    retry = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope,
        attempt_id=first['packet_ref'], _retry_of_packet_ref=first['packet_ref'])
    reviewed = repair.review_semantic_gap_replay(runtime, packet_ref=retry['packet_ref'], scope=scope)
    assert reviewed['verdict'] == 'relevant' and len(judges) == 1
    assert runtime.store.get_by_id(first['packet_ref']).to_dict() == old
    assert runtime.store.get_by_id(gap.record_id).status == 'active'


def test_stage_counts_are_numeric_only_and_missing_stays_unknown():
    b = empty_recall()
    b.explanation['query'] = 'PRIVATE'
    b.explanation['engine_diagnostics']['candidate_count'] = 7
    b.explanation['relevance_selector'].update(input_count=4, selected_count=0,
        dropped_count=4, dropped_reasons={'PRIVATE': 4})
    state = repair._replay_retrieval_state(b)
    assert {k: state[k] for k in ('candidate_count', 'selector_input_count',
        'selector_selected_count', 'selector_dropped_count')} == dict(candidate_count=7,
        selector_input_count=4, selector_selected_count=0, selector_dropped_count=4)
    assert 'PRIVATE' not in str(state)
    b.explanation['engine_diagnostics']['candidate_count'] = True
    b.explanation['relevance_selector']['input_count'] = 'PRIVATE'
    state = repair._replay_retrieval_state(b)
    assert state['candidate_count'] is None and state['selector_input_count'] is None
    assert repair._replay_retrieval_state(empty_recall())['candidate_count'] is None


def test_unresolved_empty_survives_durable_diagnostics_without_credit(replay_case, monkeypatch):
    from eimemory.scheduler.result_contract import _semantic_repair_diagnostics
    runtime, scope, gap, _, _, _, _, _ = replay_case
    monkeypatch.setattr(runtime.memory, 'recall', empty_recall)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=8)
    safe = _semantic_repair_diagnostics(report)
    assert safe['unresolved_count'] >= 1 and safe['unverifiable_count'] == 0
    assert safe['results'][0]['status'] == 'unresolved'
    assert safe['results'][0]['reason'] == 'semantic_repair_no_evidence'
    assert not findings and not safe['natural_benchmark_eligible']
    audit = runtime.store.get_by_id(safe['results'][0]['audit_ref'])
    assert audit.content['status'] == 'unresolved'
    assert audit.content['passing_resolution_created'] is False
