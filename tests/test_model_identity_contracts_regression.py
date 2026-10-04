"""Offline model/serialization contracts; synthetic proofs do not test model quality."""
from dataclasses import asdict
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope, RUNTIME_ADAPTER_CONTRACT_VERSION
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.contracts.recall_evidence import bind_selected_proofs, business_recall_supported, valid_proof
from eimemory.core.record_ids import InvalidRecordId, validate_record_id
from eimemory.evaluation import explicit_recall as explicit
from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle, compact_record
from eimemory.recall.loadout import render_loadout
from eimemory.retrieval.diagnostics import _safe_proofs
from eimemory.retrieval.evidence_fragments import evidence_fragments
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text


def _record(*, source_id='default', record_id='synthetic_1', scope=None, title='Synthetic task fact', summary='The synthetic project uses a local queue.'):
    return RecordEnvelope.from_dict({'record_id':record_id, 'kind':'memory', 'title':title,
        'summary':summary, 'content':{'text':summary}, 'source_id':source_id,
        'scope':scope or {}, 'source':'synthetic.test'})


def _proof(record, length=1):
    parent = candidate_record_keyword_text(record, max_text_chars=16000)
    return {'record_id':record.record_id, 'span_start':0, 'span_end':length,
        'quote_digest':sha256(parent[:length].encode()).hexdigest()}


def _bundle(records, proofs=None):
    explanation = {} if proofs is None else {'engine_diagnostics':{}, 'relevance_selector':{
        'status':'evidence_found', 'caller_assistance':{
            'status':'evidence_found', 'outcome':'supported', 'proofs':proofs}}}
    return RecallBundle(items=records, rules=[], reflections=[], confidence=.8,
        next_action_hint='', explanation=explanation)


@pytest.mark.parametrize('length', [96, 97, 128])
def test_compact_preserves_complete_source_partition(length):
    row = _record(source_id='a'*length)
    assert compact_record(row)['source_id'] == row.source_id
    assert RecordEnvelope.from_dict(row.to_dict()).source_id == row.source_id


def test_same_prefix_source_partitions_remain_distinct():
    left = _record(source_id='a'*127+'b')
    right = _record(source_id='a'*127+'c')
    compact = [compact_record(row) for row in (left, right)]
    assert compact[0]['source_id'] != compact[1]['source_id']
    for item in compact:
        assert sum(row.record_id == item['record_id'] and row.source_id == item['source_id']
                   for row in (left, right)) == 1
    # ID-only proofs may not disambiguate equal IDs across source partitions.
    assert bind_selected_proofs([_proof(left)], [left, right]) == []


def test_source_partition_overflow_is_still_rejected():
    with pytest.raises(ValueError, match='source_id'):
        _record(source_id='a'*129)


@pytest.mark.parametrize('length', [96, 97, 128])
def test_native_explicit_observation_keeps_long_source_references(monkeypatch, length):
    """Keep native assembly/matching; replace only external collection/signing/storage."""
    scope = {'agent_id':'synthetic-agent', 'workspace_id':'synthetic-workspace'}
    exact = resolve_channel_scope('hermes', scope)
    row = _record(source_id='a'*length, scope=exact)
    bundle = _bundle([row])
    saved = {}
    def insert(runtime, record):
        saved[record.record_id] = record
        return record, True
    monkeypatch.setattr(explicit, '_record', lambda source, rid, scope, body:
        SimpleNamespace(record_id=rid, content=body))
    monkeypatch.setattr(explicit, '_insert_once', insert)
    monkeypatch.setattr(explicit, '_verify', lambda *a:None)
    monkeypatch.setattr(explicit, '_reference', lambda runtime, record, scope:
        {'record_ref':record.record_id, 'scope':exact, 'source_id':record.source_id})
    monkeypatch.setattr(explicit, 'load_explicit_capture', lambda runtime, rid, **kw:saved[rid])
    monkeypatch.setattr(explicit, '_public_result', lambda capture:capture.content)
    service = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(get_by_exact_ref=lambda *a,**kw:row)),
        max_context_chars=7200, _positive_limit=lambda value, default:int(value),
        _assemble_recall_bundle=AgentRuntimeMemoryService._assemble_recall_bundle,
        _proactive_release=lambda *a:{})
    def prefetch(**kwargs):
        assembled = service._assemble_recall_bundle(bundle, limit=1)
        return {'ok':True, 'adapter_contract_version':RUNTIME_ADAPTER_CONTRACT_VERSION,
            'channel':'hermes', 'scope':exact, 'bundle':assembled,
            'context':render_loadout(assembled, max_chars=service.max_context_chars)}, bundle
    service._prefetch_result = prefetch
    capture = explicit.observe_explicit_recall(service, channel='hermes', scope=scope,
        query='What does the synthetic project use?', task_type='conversation', limit=1,
        explicit_request={'session_id':'synthetic-session', 'request_id':'synthetic-request', 'acceptance_generated':False})
    assert capture['result']['ok'] is True
    assert capture['error'] == ''
    assert [ref['source_id'] for ref in capture['references']] == [row.source_id]


@pytest.mark.parametrize('length', [128, 129, 256])
def test_supported_bundle_keeps_every_legal_record_id_length(length):
    row = _record(record_id='r'*length)
    proof = _proof(row)
    assert validate_record_id(row.record_id) == row.record_id
    assert valid_proof(proof)
    assert _safe_proofs([proof]) == [proof]
    out = _bundle([row], [proof]).to_compact_dict(limit=1)
    assert out['retrieval_status'] == 'evidence_found'
    assert out['recall_diagnostics']['caller_assistance']['proofs'] == [proof]
    assert business_recall_supported({'ok':True, 'bundle':out})


@pytest.mark.parametrize('record_id', ['', '.', '..', '...', '../a', 'a/b', 'a b', 'a\x1fb', '\u00e9', 'r'*257])
def test_invalid_record_ids_rejected_by_all_identity_boundaries(record_id):
    with pytest.raises(InvalidRecordId):
        _record(record_id=record_id)
    proof = {**_proof(_record()), 'record_id':record_id}
    assert not valid_proof(proof)
    assert _safe_proofs([proof]) == []
    assert bind_selected_proofs([proof], [{'record_id':record_id}]) == []


@pytest.mark.parametrize('record_id', [None, 123, True])
def test_proof_id_remains_strict_text(record_id):
    proof = {**_proof(_record()), 'record_id':record_id}
    assert not valid_proof(proof)
    assert _safe_proofs([proof]) == []


@pytest.mark.parametrize('changed', [
    {'quote_digest':'a'*65}, {'quote_digest':'A'*64}, {'span_start':-1},
    {'span_start':True}, {'span_end':16001}, {'span_end':0}, {'span_end':1.0},
])
def test_proof_nonidentity_bounds_do_not_expand(changed):
    proof = {**_proof(_record(record_id='r'*256)), **changed}
    assert not valid_proof(proof)
    assert _safe_proofs([proof]) == []


def test_long_id_proofs_keep_exact_binding_and_collection_limits():
    rows = [_record(record_id='r'*255+str(index)) for index in range(9)]
    proofs = [_proof(row) for row in rows]
    assert bind_selected_proofs(proofs[:8], rows[:8]) == proofs[:8]
    assert bind_selected_proofs(proofs, rows) == []
    assert bind_selected_proofs([proofs[0], proofs[0]], rows[:1]) == []
    assert bind_selected_proofs([proofs[0]], rows[:2]) == [proofs[0]]
    assert bind_selected_proofs([proofs[0]], rows[1:2]) == []
    assert _safe_proofs(proofs) == proofs[:3]
    assert 'quote' not in _safe_proofs([{**proofs[0], 'quote':'private synthetic text'}])[0]
    other_scope = _record(record_id=rows[0].record_id, scope={'agent_id':'other'})
    assert bind_selected_proofs([proofs[0]], [rows[0], other_scope]) == []


@pytest.mark.parametrize('mismatch', ['none', 'scope', 'source'])
def test_fragment_binding_still_requires_exact_scope_and_source(mismatch):
    row = _record(source_id='a'*127+'b', scope={'agent_id':'synthetic-agent'})
    parent = candidate_record_keyword_text(row, max_text_chars=16000)
    fragment = evidence_fragments(parent)[0]
    scored = {'record_id':row.record_id, 'source_id':row.source_id, 'scope':asdict(row.scope),
        'admitted':True, 'fragment_id':fragment['id']}
    if mismatch == 'scope':
        scored['scope'] = asdict(ScopeRef(agent_id='other'))
    elif mismatch == 'source':
        scored['source_id'] = 'a'*127+'c'
    bundle = _bundle([row])
    bundle.explanation = {'relevance_selector':{'status':'evidence_found', 'scored':[scored]}}
    out = bundle.to_compact_dict(limit=1)
    assert ('evidence_excerpt' in out['items'][0]) is (mismatch == 'none')


@pytest.mark.parametrize('limit, ceiling', [(1, 4096), (5, 16384)])
def test_maximum_identity_lengths_preserve_compact_budget(limit, ceiling):
    rows = [_record(record_id='r'*255+str(index), source_id='s'*127+str(index),
        title='\u6807\u9898'*2500, summary='\u957f\u8bc1\u636e'*2000) for index in range(5)]
    proofs = [_proof(row, length=768) for row in rows]
    out = _bundle(rows, proofs).to_compact_dict(limit=limit, include_explanation=True)
    assert len(json.dumps(out, ensure_ascii=False, separators=(',', ':')).encode()) <= ceiling
    assert [item['record_id'] for item in out['items']] == [row.record_id for row in rows[:limit]]
    assert [item['source_id'] for item in out['items']] == [row.source_id for row in rows[:limit]]
    assert business_recall_supported({'ok':True, 'bundle':out})
    assert out['recall_diagnostics']['caller_assistance']['proofs'] == proofs[:min(limit, 3)]


def _oversized_bundle(count=30):
    rows = [_record(record_id='r'*254+f'{index:02d}', source_id='s'*126+f'{index:02d}',
        title='t'*120, summary='s'*240) for index in range(count)]
    return _bundle(rows, [_proof(row) for row in rows[:3]])


def _runtime_returning(bundle):
    return SimpleNamespace(memory=SimpleNamespace(recall=lambda **kw:bundle))


def _assert_budget_error(result, selected_count):
    assert result['ok'] is False
    assert result['error'] == 'compact_payload_too_large'
    assert result['retrieval_status'] == 'unavailable'
    assert result['size_budget']['selected_count'] == selected_count
    assert result['size_budget']['delivered_count'] == 0
    assert result['size_budget']['payload_bytes'] > result['size_budget']['maximum_bytes']
    assert not {'bundle', 'proofs', 'items', 'context'} & result.keys()
    assert 'no_evidence' not in json.dumps(result)
    assert len(json.dumps(result)) < 4096


@pytest.mark.parametrize('limit', [30, 50])
def test_unfittable_identity_selection_fails_closed_without_changing_records(limit):
    from eimemory.models.records import CompactRecallBudgetExceeded
    bundle = _oversized_bundle(limit)
    original = bundle.to_dict()
    with pytest.raises(CompactRecallBudgetExceeded) as caught:
        bundle.to_compact_dict(limit=limit)
    _assert_budget_error(caught.value.to_dict(), limit)
    assert str(caught.value) == 'compact_payload_too_large'
    assert bundle.to_dict() == original


def test_compact_size_ceiling_is_inclusive_and_overflow_is_structured():
    from eimemory.models.records import CompactRecallBudgetExceeded, _fit_compact_payload
    payload = {'items':[{'record_id':'r'*256,'source_id':'s'*128,'title':'','summary':''}],
        'rules':[], 'reflections':[]}
    size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
    assert _fit_compact_payload(payload, maximum_bytes=size) is payload
    with pytest.raises(CompactRecallBudgetExceeded) as caught:
        _fit_compact_payload(payload, maximum_bytes=size-1)
    _assert_budget_error(caught.value.to_dict(), 1)
    assert caught.value.payload_bytes == size
    assert payload['items'][0]['record_id'] == 'r'*256
    assert payload['items'][0]['source_id'] == 's'*128


def test_native_service_and_rpc_return_budget_failure_without_delivery():
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    bundle = _oversized_bundle()
    runtime = _runtime_returning(bundle)
    service = AgentRuntimeMemoryService(runtime)
    params = {'channel':'hermes','scope':{'agent_id':'synthetic-agent'},'query':'synthetic query','limit':30}
    _assert_budget_error(service.prefetch(**params), 30)
    out = EIBrainRPCBridge(runtime).handle({'method':'adapter.prefetch','params':params})
    assert out['ok'] is False
    _assert_budget_error(out['result'], 30)


def test_native_search_l0_catches_its_own_compact_budget_failure():
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    row = _record()
    # Synthetic oversized presentation metadata exercises the <=2-result caller.
    row.provenance = {'project_context':{'schema':'same_turn_release_context.v1','synthetic_padding':'x'*20000}}
    runtime = _runtime_returning(_bundle([row], [_proof(row)]))
    params = {'channel':'hermes','scope':{'agent_id':'synthetic-agent'},'query':'synthetic query','limit':2}
    _assert_budget_error(AgentRuntimeMemoryService(runtime).search_l0(**params), 1)
    out = EIBrainRPCBridge(runtime).handle({'method':'adapter.search_l0','params':params})
    assert out['ok'] is False
    _assert_budget_error(out['result'], 1)


@pytest.mark.parametrize('failure', ['budget', 'unrelated'])
def test_native_explicit_capture_preserves_error_category_and_no_references(monkeypatch, failure):
    bundle = _oversized_bundle()
    saved = {}
    def insert(runtime, record):
        saved[record.record_id] = record
        return record, True
    monkeypatch.setattr(explicit, '_record', lambda source, rid, scope, body:
        SimpleNamespace(record_id=rid, content=body))
    monkeypatch.setattr(explicit, '_insert_once', insert)
    monkeypatch.setattr(explicit, '_verify', lambda *a:None)
    monkeypatch.setattr(explicit, 'load_explicit_capture', lambda runtime, rid, **kw:saved[rid])
    service = AgentRuntimeMemoryService(_runtime_returning(bundle))
    monkeypatch.setattr(service, '_proactive_release', lambda *a:{})
    if failure == 'unrelated':
        def invalid(*args, **kwargs):
            raise ValueError('unrelated_synthetic_failure')
        monkeypatch.setattr(RecallBundle, 'to_compact_dict', invalid)
    out = service.prefetch(channel='hermes', scope={'agent_id':'synthetic-agent'}, query='synthetic query', limit=30,
        explicit_request={'session_id':'synthetic-session','request_id':'synthetic-request','acceptance_generated':False})
    captures = [row for row in saved.values() if 'result' in row.content]
    assert len(captures) == 1
    assert captures[0].content['references'] == []
    if failure == 'budget':
        _assert_budget_error(out, 30)
        assert captures[0].content['error'] == 'compact_payload_too_large'
        _assert_budget_error(captures[0].content['result'], 30)
    else:
        assert out['ok'] is False and out['error'] == 'ValueError'
        assert 'size_budget' not in out
        assert captures[0].content['error'] == 'ValueError'


def test_native_cli_compact_returns_budget_json_and_nonzero_exit(capsys):
    from eimemory.cli.main import _dispatch_recall
    parsed = SimpleNamespace(view='', compact=True, query='synthetic query', limit=30, explain=False)
    assert _dispatch_recall(parsed, _runtime_returning(_oversized_bundle()), {}) == 1
    captured = capsys.readouterr()
    _assert_budget_error(json.loads(captured.out), 30)
    assert captured.err == ''


@pytest.mark.parametrize('consumer', ['prefetch', 'search_l0', 'cli'])
def test_budget_handlers_do_not_swallow_unrelated_value_errors(monkeypatch, consumer):
    from eimemory.cli.main import _dispatch_recall
    def invalid(*args, **kwargs):
        raise ValueError('unrelated_synthetic_failure')
    monkeypatch.setattr(RecallBundle, 'to_compact_dict', invalid)
    runtime = _runtime_returning(_bundle([_record()]))
    with pytest.raises(ValueError, match='^unrelated_synthetic_failure$'):
        if consumer == 'cli':
            parsed = SimpleNamespace(view='', compact=True, query='synthetic query', limit=1, explain=False)
            _dispatch_recall(parsed, runtime, {})
        else:
            getattr(AgentRuntimeMemoryService(runtime), consumer)(channel='hermes', scope={}, query='synthetic query', limit=1)


def test_success_shape_stays_unchanged_for_service_search_and_cli(capsys):
    from eimemory.cli.main import _dispatch_recall
    runtime = _runtime_returning(_bundle([_record()]))
    service = AgentRuntimeMemoryService(runtime)
    out = service.prefetch(channel='hermes',scope={},query='synthetic query',limit=1)
    assert set(out) == {'ok','adapter_contract_version','channel','scope','bundle','context'}
    assert out['ok'] is True and 'size_budget' not in out
    search = service.search_l0(channel='hermes',scope={},query='synthetic query',limit=1)
    assert set(search) == {'ok','channel','scope','bundle'}
    assert search['ok'] is True
    parsed = SimpleNamespace(view='',compact=True,query='synthetic query',limit=1,explain=False)
    assert _dispatch_recall(parsed,runtime,{}) == 0
    assert json.loads(capsys.readouterr().out)['schema_version'] == 'recall_bundle.compact.v1'


def _final_budget_rows(count, scope=None):
    return [RecordEnvelope.from_dict({'record_id':'R'*253+f'{index:03d}',
        'source_id':'s'*125+f'{index:03d}', 'kind':'memory', 'source':'synthetic',
        'title':'T'*32, 'summary':'S'*32, 'scope':scope or {}}) for index in range(count)]


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def test_final_loadout_rejects_reviewers_29_row_overflow():
    bundle = _bundle(_final_budget_rows(29))
    before = bundle.to_dict()
    assert _encoded_size(bundle.to_compact_dict(limit=29)) == 16333
    result = AgentRuntimeMemoryService(_runtime_returning(bundle)).prefetch(
        channel='hermes', scope={}, query='synthetic', limit=29)
    _assert_budget_error(result, 29)
    assert result['size_budget'] == {
        'maximum_bytes':16384, 'payload_bytes':16539, 'selected_count':29, 'delivered_count':0}
    assert bundle.to_dict() == before


def test_top1_final_loadout_uses_4k_after_larger_candidate_pool():
    row = _final_budget_rows(1)[0]
    row.provenance = {'project_context':{'schema':'same_turn_release_context.v1','padding':''}}
    bundle = _bundle([row])
    base_size = _encoded_size(bundle.to_compact_dict(limit=1))
    row.provenance['project_context']['padding'] = 'x'*(4096-base_size)
    assert _encoded_size(bundle.to_compact_dict(limit=1)) == 4096
    result = AgentRuntimeMemoryService(_runtime_returning(bundle)).prefetch(
        channel='hermes', scope={}, query='synthetic', limit=1)
    _assert_budget_error(result, 1)
    assert result['size_budget']['maximum_bytes'] == 4096
    assert result['size_budget']['payload_bytes'] == 4302


def test_final_validator_is_nonmutating_utf8_and_counts_persona():
    from copy import deepcopy
    from eimemory.models.records import CompactRecallBudgetExceeded, validate_compact_payload_budget
    payload = {'items':[{'record_id':'synthetic_a','summary':'\u754c'*8}],
        'persona':[{'record_id':'synthetic_b','summary':'\u754c'*8}]}
    original = deepcopy(payload)
    size = _encoded_size(payload)
    validate_compact_payload_budget(payload, maximum_bytes=size)
    with pytest.raises(CompactRecallBudgetExceeded) as caught:
        validate_compact_payload_budget(payload, maximum_bytes=size-1)
    _assert_budget_error(caught.value.to_dict(), 2)
    assert caught.value.payload_bytes == size
    assert payload == original


def test_explicit_final_scope_additions_are_rechecked_before_capture_success(monkeypatch):
    exact = resolve_channel_scope('hermes', {})
    rows = _final_budget_rows(28, scope=exact)
    by_id = {row.record_id:row for row in rows}
    bundle = _bundle(rows)
    service = AgentRuntimeMemoryService(SimpleNamespace(
        memory=SimpleNamespace(recall=lambda **kw:bundle),
        store=SimpleNamespace(get_by_exact_ref=lambda rid, **kw:by_id[rid])))
    assembled = service._assemble_recall_bundle(bundle, limit=28)
    assert _encoded_size(assembled) <= 16384
    # The native explicit path attaches these exact scope references to each item.
    for item in assembled['items']:
        item['scope'] = exact
    expected_final_size = _encoded_size(assembled)
    assert expected_final_size > 16384
    saved = {}
    def insert(runtime, record):
        saved[record.record_id] = record
        return record, True
    monkeypatch.setattr(explicit, '_record', lambda source, rid, scope, body:
        SimpleNamespace(record_id=rid, content=body))
    monkeypatch.setattr(explicit, '_insert_once', insert)
    monkeypatch.setattr(explicit, '_verify', lambda *a:None)
    monkeypatch.setattr(explicit, '_reference', lambda runtime, record, scope:
        {'record_ref':record.record_id,'scope':exact,'source_id':record.source_id})
    monkeypatch.setattr(explicit, 'load_explicit_capture', lambda runtime, rid, **kw:saved[rid])
    monkeypatch.setattr(service, '_proactive_release', lambda *a:{})
    out = service.prefetch(channel='hermes', scope={}, query='synthetic', limit=28,
        explicit_request={'session_id':'synthetic-session','request_id':'synthetic-request','acceptance_generated':False})
    _assert_budget_error(out, 28)
    assert out['size_budget']['payload_bytes'] == expected_final_size
    capture = saved[out['capture']['record_id']].content
    assert capture['references'] == []
    assert capture['error'] == 'compact_payload_too_large'
    assert 'bundle' not in capture['result']


def test_cli_has_no_post_compaction_bundle_growth(capsys):
    from eimemory.cli.main import _dispatch_recall
    bundle = _bundle(_final_budget_rows(29))
    expected = bundle.to_compact_dict(limit=29)
    parsed = SimpleNamespace(view='', compact=True, query='synthetic', limit=29, explain=False)
    assert _dispatch_recall(parsed, _runtime_returning(bundle), {}) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == expected
    assert _encoded_size(output) == 16333
