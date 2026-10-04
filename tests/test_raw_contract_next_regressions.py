"""Public raw identity, completion, and context regressions (synthetic only)."""
from collections import Counter
from dataclasses import asdict
from time import perf_counter

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.boundary import RawRecallUnavailable, guarded_raw_search, raw_request_boundary
from eimemory.raw.retrieval import search_raw_chunks
from eimemory.raw.store import RawEvidenceAPI
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id='fixture')

@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path)
    yield value
    value.close()


def raw_record(store, *, source_id='alpha', event='event', session='session', index=0, status='active', scope=SCOPE):
    return store.append(RecordEnvelope.create(
        kind='raw_chunk', title='synthetic raw', scope=scope, source_id=source_id,
        status=status, content={'text': 'cedar fixture evidence', 'session_id': session,
                                'source_event_id': event, 'chunk_index': index}))


@pytest.mark.parametrize('role', ['assistant', 'tool', '', 'unknown', 'user'])
def test_public_raw_search_keeps_original_text_and_attribution(store, role):
    text = 'I prefer synthetic cedar prose.'
    record = RawEvidenceAPI(store).ingest_text(text=text, scope=SCOPE,
        source_event_id='speaker-event', session_id='speaker-session', role=role, speaker=role or 'unknown')[0]
    result = search_raw_chunks(store, query='cedar', scope=SCOPE, limit=3)[0]['record']
    assert result['record_id'] == record.record_id
    assert result['text'] == text
    assert result['role'] == role
    assert result['speaker'] == (role or 'unknown')
    assert result['source_event_id'] == 'speaker-event'
    assert result['chunk_index'] == '0'


def test_public_memory_raw_hybrid_preserves_assistant_attribution(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        text = 'I prefer synthetic cedar prose.'
        runtime.raw.ingest_text(text=text,scope=SCOPE,source_event_id='event',session_id='session',role='assistant',speaker='fixture-agent')
        bundle = runtime.memory.recall(query='cedar prose',scope=asdict(SCOPE),task_context={'task_type':'chat.reply','recall_mode':'raw_hybrid'},limit=3)
        result=bundle.explanation['raw_evidence'][0]['record']
        assert result['text'] == text
        assert result['role'] == 'assistant' and result['speaker'] == 'fixture-agent'
    finally:
        runtime.close()


@pytest.mark.parametrize('alias', ['alpha','ALPHA','ＡＬＰＨＡ'])
def test_source_allowlist_aliases_match_same_authority(store, alias):
    record=raw_record(store)
    result=search_raw_chunks(store,query='cedar',scope=SCOPE,source_ids=[alias])
    assert [x['record']['record_id'] for x in result] == [record.record_id]


def test_nested_allowlist_intersects_after_normalization(store):
    record=raw_record(store)
    raw_record(store,source_id='beta')
    @raw_request_boundary
    def outer(store, **kwargs):
        return search_raw_chunks(store,query='cedar',scope=SCOPE,source_ids=['alpha','beta'])
    result=outer(store,scope=SCOPE,source_ids=['ＡＬＰＨＡ'])
    assert [x['record']['record_id'] for x in result] == [record.record_id]


def test_empty_and_disjoint_allowlists_stay_empty(store):
    raw_record(store)
    assert search_raw_chunks(store,query='cedar',scope=SCOPE,source_ids=[]) == []
    @raw_request_boundary
    def outer(store, **kwargs):
        return search_raw_chunks(store,query='cedar',scope=SCOPE,source_ids=['beta'])
    assert outer(store,scope=SCOPE,source_ids=['alpha']) == []
    with pytest.raises(ValueError,match='collision'):
        search_raw_chunks(store,query='cedar',scope=SCOPE,source_ids=['alpha','ALPHA'])


def fail_read(**kwargs):
    raise OSError('synthetic collection failure')


def test_all_collection_failures_signal_unavailable(store,monkeypatch):
    for name in ('search_with_diagnostics','search','list_records'):
        monkeypatch.setattr(store,name,fail_read)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE)
    diagnostics=Counter()
    assert guarded_raw_search(store,diagnostics=diagnostics,query='cedar',scope=SCOPE)==[]
    assert diagnostics['raw_collection_unavailable']==1


def test_successful_empty_collection_is_not_unavailable(store):
    diagnostics=Counter()
    assert guarded_raw_search(store,diagnostics=diagnostics,query='cedar',scope=SCOPE)==[]
    assert not diagnostics


def test_complete_authoritative_scan_recovers_failed_index(store,monkeypatch):
    record=raw_record(store)
    monkeypatch.setattr(store,'search_with_diagnostics',fail_read)
    monkeypatch.setattr(store,'search',fail_read)
    diagnostics=Counter()
    result=guarded_raw_search(store,diagnostics=diagnostics,query='cedar',scope=SCOPE)
    assert [x['record']['record_id'] for x in result]==[record.record_id]
    assert not diagnostics


def test_partial_collection_cannot_silently_certify_empty(store,monkeypatch):
    monkeypatch.setattr(store,'search_with_diagnostics',fail_read)
    monkeypatch.setattr(store,'search',lambda **kwargs: [])
    monkeypatch.setattr(store,'list_records',fail_read)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE)


def test_truncated_recovery_scan_is_unavailable(store,monkeypatch):
    record=raw_record(store)
    monkeypatch.setattr(store,'search_with_diagnostics',fail_read)
    monkeypatch.setattr(store,'search',fail_read)
    monkeypatch.setattr(store,'list_records',lambda **kwargs: [record]*kwargs['limit'])
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE)


def test_deadline_report_requires_complete_recovery(store,monkeypatch):
    monkeypatch.setattr(store,'search_with_diagnostics',lambda **kwargs: ([],{'retrieval_mode':'deadline_exhausted','blocked_counts':{'recall_budget_exhausted':1}}))
    monkeypatch.setattr(store,'search',lambda **kwargs: [])
    monkeypatch.setattr(store,'list_records',fail_read)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE,task_context={'_recall_deadline_monotonic':perf_counter()-1})


def test_context_is_event_local_and_radius_zero_is_anchor(store):
    raw=RawEvidenceAPI(store)
    first=raw.ingest_text(text='cedar first event',scope=SCOPE,session_id='same-session',source_event_id='first')[0]
    raw.ingest_text(text='cedar second event',scope=SCOPE,session_id='same-session',source_event_id='second')
    assert [x.record_id for x in raw.context_window(first.record_id,scope=SCOPE,radius=0)]==[first.record_id]
    assert [x.record_id for x in raw.context_window(first.record_id,scope=SCOPE,radius=1)]==[first.record_id]


def test_context_rejects_different_source_scope_and_nonactive_neighbors(store):
    center=raw_record(store,index=1)
    left=raw_record(store,index=0)
    raw_record(store,index=2,source_id='beta')
    raw_record(store,index=2,status='superseded')
    raw_record(store,index=2,scope=ScopeRef(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id=''))
    result=RawEvidenceAPI(store).context_window(center.record_id,scope=SCOPE,radius=1)
    assert [x.record_id for x in result]==[left.record_id,center.record_id]


def test_context_rejects_nonactive_anchor_and_unknown_event_expansion(store):
    inactive=raw_record(store,status='superseded')
    assert RawEvidenceAPI(store).context_window(inactive.record_id,scope=SCOPE)==[]
    center=raw_record(store,event='',index=1)
    raw_record(store,event='',index=0)
    assert [x.record_id for x in RawEvidenceAPI(store).context_window(center.record_id,scope=SCOPE)]==[center.record_id]


@pytest.mark.parametrize('report', [
    {'retrieval_mode': 'deadline_exhausted'}, {'status': 'unavailable'},
    {'status': 'degraded'}, {'degraded': True}, {'collection_complete': False},
    {'blocked_counts': {'candidate_scoring_timeout': 1}}, {'error': 'synthetic failure'},
])
def test_raw_api_preserves_incomplete_diagnostic_reports(store, monkeypatch, report):
    monkeypatch.setattr(store, 'search_with_diagnostics', lambda **kwargs: ([], report))
    with pytest.raises(RawRecallUnavailable):
        RawEvidenceAPI(store).search_raw_chunks(query='cedar', scope=SCOPE)


def test_complete_empty_scan_can_recover_failed_index(store, monkeypatch):
    monkeypatch.setattr(store, 'search_with_diagnostics', fail_read)
    diagnostics = Counter()
    assert guarded_raw_search(store, diagnostics=diagnostics, query='cedar', scope=SCOPE) == []
    assert not diagnostics


def test_partial_live_results_are_not_reported_complete_when_backstop_fails(store, monkeypatch):
    raw_record(store)
    monkeypatch.setattr(store, 'list_records', fail_read)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store, query='cedar', scope=SCOPE, limit=2)


def test_authority_read_failure_is_not_empty_evidence(store, monkeypatch):
    raw_record(store)
    monkeypatch.setattr(store, 'get_by_exact_ref', lambda *args, **kwargs: fail_read())
    diagnostics = Counter()
    assert guarded_raw_search(store, diagnostics=diagnostics, query='cedar', scope=SCOPE) == []
    assert diagnostics['raw_collection_unavailable'] == 1


def test_recovery_does_not_admit_structured_memory_as_raw(store, monkeypatch):
    store.append(RecordEnvelope.create(kind='memory', title='cedar structured fact',
        summary='cedar structured fact', content={'text':'cedar structured fact'},
        meta={'force_capture':True}, scope=SCOPE))
    monkeypatch.setattr(store, 'search_with_diagnostics', fail_read)
    assert search_raw_chunks(store, query='cedar', scope=SCOPE) == []


def test_context_explicit_user_scope_does_not_resolve_shared_anchor(store):
    shared = raw_record(store, scope=ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id=''))
    assert RawEvidenceAPI(store).context_window(shared.record_id, scope=SCOPE) == []


def test_raw_text_is_not_reconstructed_from_summary_or_title(store):
    text = '  source line one\nsource line two  '
    record = store.append(RecordEnvelope.create(kind='raw_chunk', title='different title',
        summary='different summary', detail='different detail', scope=SCOPE,
        content={'raw_text':text, 'text':'alternate text', 'role':'tool'}))
    result = search_raw_chunks(store, query='source line', scope=SCOPE)[0]['record']
    assert result['record_id'] == record.record_id
    assert result['text'] == text


def test_short_scan_that_omits_stored_rows_is_not_complete_recovery(store, monkeypatch):
    raw_record(store)
    monkeypatch.setattr(store, 'search_with_diagnostics', fail_read)
    monkeypatch.setattr(store, 'list_records', lambda **kwargs: [])
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store, query='cedar', scope=SCOPE)


def test_recovery_requires_every_authorized_scan_reference_to_hydrate(store, monkeypatch):
    raw_record(store)
    monkeypatch.setattr(store, 'search_with_diagnostics', fail_read)
    monkeypatch.setattr(store, 'get_by_exact_ref', lambda *args, **kwargs: None)
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store, query='cedar', scope=SCOPE)


def _damage_raw_payload(store, record, damage):
    import json
    payload = asdict(record)
    if damage == 'json':
        payload = {}
    elif damage == 'scope':
        payload['scope']['user_id'] = 'other-payload-user'
    elif damage == 'status':
        payload['status'] = 'superseded'
    elif damage == 'kind':
        payload['kind'] = 'reflection'
    with store._lock:
        store.sqlite.conn.execute(
            "UPDATE records SET payload_json=?,payload_pointer_json='',payload_digest='' WHERE record_id=?",
            (json.dumps(payload), record.record_id),
        )
        store.sqlite.conn.commit()


@pytest.mark.parametrize('kind', ['raw_chunk', 'memory'])
@pytest.mark.parametrize('damage', ['json', 'scope', 'status', 'kind'])
@pytest.mark.parametrize('exact', [False, True])
def test_actual_damaged_raw_representations_do_not_certify_absence(tmp_path, kind, damage, exact):
    runtime = Runtime.create(root=tmp_path)
    try:
        record = runtime.store.append(RecordEnvelope.create(kind=kind,
            title='cedar nebula original evidence', scope=SCOPE,
            content={'raw_text':'cedar nebula original evidence'}, meta={'force_capture':True}))
        _damage_raw_payload(runtime.store, record, damage)
        records, report = runtime.store.search_with_diagnostics(query='cedar nebula', kinds=[kind], scope=SCOPE)
        reason = 'corrupt_record' if damage == 'json' else 'projection_payload_mismatch'
        assert report['blocked_counts'][reason] == 1
        if kind == 'raw_chunk':
            with pytest.raises(RawRecallUnavailable):
                runtime.raw.search_raw_chunks(query='cedar nebula', scope=SCOPE)
        diagnostics = Counter()
        assert guarded_raw_search(runtime.store, diagnostics=diagnostics, query='cedar nebula', scope=SCOPE,
            task_context={'exact_scope_only':exact}) == []
        assert diagnostics['raw_collection_unavailable'] == 1
        bundle = runtime.memory.recall(query='cedar nebula', scope=asdict(SCOPE),
            task_context={'task_type':'chat.reply','recall_mode':'raw_hybrid','scope_strategy':'exact' if exact else 'legacy_union'}, limit=3)
        assert bundle.explanation['retrieval_status'] == 'unavailable'
        assert bundle.explanation['relevance_selector']['collection_complete'] is False
    finally:
        runtime.close()


@pytest.mark.parametrize('damage', ['json', 'scope', 'status', 'kind'])
@pytest.mark.parametrize('exact', [False, True])
def test_normal_sparse_backstop_validates_actual_damaged_rows(store, monkeypatch, damage, exact):
    record=raw_record(store)
    _damage_raw_payload(store,record,damage)
    # Route through the normal sparse backstop. Only index results are stubbed;
    # corruption, SQL listing/counting, and authority hydration are real.
    monkeypatch.setattr('eimemory.raw.retrieval._raw_api_search', lambda *a, **kw: [])
    monkeypatch.setattr('eimemory.raw.retrieval._store_raw_candidates', lambda *a, **kw: [])
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='quuxnevermatchedword',scope=SCOPE,task_context={'exact_scope_only':exact})


@pytest.mark.parametrize('reason', [
    'request_scope_mismatch', 'request_kind_mismatch', 'request_source_mismatch',
    'inactive_record', 'legacy_lane_filtered', 'legacy_visibility_filtered',
    'quality_rejected', 'lexical_grounding_missing', 'insufficient_lexical_grounding',
])
def test_documented_candidate_exclusions_remain_completed(store, monkeypatch, reason):
    monkeypatch.setattr(store,'search_with_diagnostics',lambda **kwargs: ([],{'blocked_counts':{reason:1}}))
    assert RawEvidenceAPI(store).search_raw_chunks(query='cedar',scope=SCOPE)==[]


def test_unknown_blocked_reason_cannot_claim_complete(store, monkeypatch):
    monkeypatch.setattr(store,'search_with_diagnostics',lambda **kwargs: ([],{'blocked_counts':{'future_unclassified_storage_failure':1}}))
    with pytest.raises(RawRecallUnavailable):
        RawEvidenceAPI(store).search_raw_chunks(query='cedar',scope=SCOPE)


def test_exact_scope_excludes_valid_shared_record_without_unavailable(store):
    raw_record(store,scope=ScopeRef(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id=''))
    diagnostics=Counter()
    assert guarded_raw_search(store,diagnostics=diagnostics,query='cedar',scope=SCOPE,
        task_context={'exact_scope_only':True})==[]
    assert not diagnostics


def test_real_quality_exclusion_is_not_read_failure(store):
    store.append(RecordEnvelope.create(kind='raw_chunk',title='cedar evidence',scope=SCOPE,
        content={'raw_text':'cedar evidence'},meta={'quality':{'capture_decision':'reject'}}))
    _, report=store.search_with_diagnostics(query='cedar',kinds=['raw_chunk'],scope=SCOPE)
    assert report['blocked_counts']['quality_rejected']==1
    diagnostics=Counter()
    assert guarded_raw_search(store,diagnostics=diagnostics,query='cedar',scope=SCOPE)==[]
    assert not diagnostics


def test_report_cannot_turn_integrity_reason_into_a_policy_lane(store,monkeypatch):
    report={'blocked_counts':{'corrupt_record':1},'recall_filters':{'blocked_recall_lanes':['corrupt_record']}}
    monkeypatch.setattr(store,'search_with_diagnostics',lambda **kw:([],report))
    with pytest.raises(RawRecallUnavailable):
        search_raw_chunks(store,query='cedar',scope=SCOPE)
