import json

from eimemory.models.records import RecallBundle


def test_compact_diagnostics_are_bounded_and_allowlisted():
    bundle = RecallBundle(items=[], rules=[], reflections=[], confidence=0, next_action_hint='', explanation={
        'query': 'PRIVATE QUERY',
        'engine_diagnostics': {
            'elapsed_ms': 2000,
            'source_searches': 34,
            'source_budget_exhausted': 1,
            'source_stages_ms': {'sqlite': 600, 'embedding_wait': 500, 'PRIVATE_STAGE': 3},
            'drops': {'candidate_hydration_timeout': 8, 'SECRET': 2},
            'source_samples': [{'error_code': 'SECRET', 'scope': 'PRIVATE_SCOPE'}] * 100,
        },
        'relevance_selector': {'status': 'unavailable', 'elapsed_ms': 250,
            'dropped_reasons': {'admission_deadline_exceeded': 1, 'SECRET': 5}},
    })
    result = bundle.to_compact_dict(limit=1)['recall_diagnostics']
    assert result['source_searches'] == 34
    assert result['source_budget_exhausted'] == 1
    assert result['stages_ms'] == {'sqlite': 600, 'embedding_wait': 500}
    assert result['admission_drops'] == {'admission_deadline_exceeded': 1}
    assert result['engine_drops'] == {'candidate_hydration_timeout': 8}
    assert 'PRIVATE' not in json.dumps(result)
    assert 'SECRET' not in json.dumps(result)
    assert len(json.dumps(result)) < 1500


def test_legacy_bundle_does_not_invent_diagnostics_or_task_state():
    result = RecallBundle(items=[], rules=[], reflections=[], confidence=0, next_action_hint='').to_compact_dict()
    assert 'recall_diagnostics' not in result
    assert 'task_evidence_scope' not in result
