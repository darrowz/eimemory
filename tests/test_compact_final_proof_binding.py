from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService


def proof(record):
    return {'record_id': record.record_id, 'span_start': 0, 'span_end': 1, 'quote_digest': 'a' * 64}


def bundle(items, *, status='evidence_found', proofs=None):
    return RecallBundle(items=items, rules=[], reflections=[], confidence=0.8,
        next_action_hint='', explanation={'engine_diagnostics': {}, 'relevance_selector': {'status': status,
        'caller_assistance': {'status': 'evidence_found', 'outcome': 'supported',
                              'proofs': proofs or []}}})


def test_empty_final_payload_cannot_keep_nested_supported_proof():
    record = RecordEnvelope.create(kind='memory', scope=ScopeRef(), title='synthetic')
    out = bundle([], status='unavailable', proofs=[proof(record)]).to_compact_dict()
    caller = out['recall_diagnostics']['caller_assistance']
    assert caller['outcome'] == 'unavailable'
    assert not caller.get('proofs')
    assert out['recall_diagnostics']['selected_count'] == 0


def test_compact_limit_removes_proof_for_nonreturned_item():
    a, b = [RecordEnvelope.create(kind='memory', scope=ScopeRef(), title=title) for title in ['first', 'second']]
    out = bundle([a, b], proofs=[proof(b)]).to_compact_dict(limit=1)
    assert [r['record_id'] for r in out['items']] == [a.record_id]
    assert not out['recall_diagnostics']['caller_assistance'].get('proofs')


def test_service_loadout_rebinds_proofs_after_final_limit():
    a, b = [RecordEnvelope.create(kind='memory', scope=ScopeRef(), title=title, meta={'memory_type': 'fact'})
            for title in ['first', 'second']]
    out = AgentRuntimeMemoryService._assemble_recall_bundle(bundle([a, b], proofs=[proof(b)]), limit=1)
    assert [r['record_id'] for r in out['items']] == [a.record_id]
    assert not out['recall_diagnostics']['caller_assistance'].get('proofs')


def test_supported_returned_record_keeps_valid_proof():
    r = RecordEnvelope.create(kind='memory', scope=ScopeRef(), title='synthetic')
    out = bundle([r], proofs=[proof(r)]).to_compact_dict(limit=1)
    assert out['recall_diagnostics']['caller_assistance']['proofs'] == [proof(r)]
