"""Quality outages cannot change recall authority or mint model proofs."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from test_recall_engine import SCOPE, FakeCandidateSource, _hit, _record
from test_proactive_recall import BASE_SCOPE, _service, _record as proactive_record
from eimemory.api.memory import MemoryAPI
from eimemory.contracts.recall_evidence import business_recall_supported
from eimemory.models.records import RecallBundle
from eimemory.retrieval import caller_assistance
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.relevance import RelevanceAdmission, RelevanceConfig
from eimemory.retrieval.evidence_fragments import evidence_fragments, POLICY
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('failure', [TimeoutError, RuntimeError])
@pytest.mark.parametrize('mode', ['legacy', 'lightweight', 'reranker'])
def test_quality_failure_never_runs_on_selector_path(tmp_path, monkeypatch, failure, mode):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise failure('quality provider unavailable')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setattr(caller_assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=1, complete=fail))
    with closing(RuntimeStore(tmp_path)) as store:
        row = store.append(_record(text='release process requires pytest before publishing'))
        engine = GovernedRecallEngine(store=store, candidate_source=None)
        engine.relevance_admission = None
        key = engine._record_key(row)
        hints = {key: {'lexical_score': 1.0}}
        if mode == 'reranker':
            engine.relevance_admission = RelevanceAdmission(RelevanceConfig(enabled=True, api_key='test', revision='a' * 40), scorer=SimpleNamespace(score=fail))
        if mode == 'lightweight':
            selector = LightweightAdmission(LightweightConfig(enabled=True))
            text = candidate_record_keyword_text(row, max_text_chars=16000)
            fragment = evidence_fragments(text)[0]
            chosen, state = selector.select([row, row], query='release process pytest', limit=1,
                validate=lambda r: r == row, backend_available=True,
                hints_for=lambda r: {'fragment_policy': POLICY, 'evidence_fragment_id': fragment['id'],
                                    'dense_vector_score': .9, 'fragment_fts_score': .9})
        else:
            chosen, state = engine._select_post_fusion_items([row, row], query='release process pytest',
                limit=1, fusion_state={}, component_hints_by_ref=hints)
        assert chosen == [row]
        assert calls == [], 'quality evaluation must be deferred, not caught after waiting'
        assert not state.get('caller_assistance', {}).get('proofs')
        bundle = RecallBundle(chosen, [], [], .8, '', {'relevance_selector': state})
        compact = bundle.to_compact_dict(limit=1)
        assert compact['items'] and compact['retrieval_status'] == 'evidence_found'
        assert not business_recall_supported({'ok': True, 'bundle': compact})


@pytest.mark.parametrize('case', ['valid', 'cross_scope', 'revoked', 'source', 'empty'])
def test_memory_api_authority_and_empty_results(tmp_path, monkeypatch, case):
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    def fail():
        raise AssertionError('recall must not even prepare a quality provider')
    monkeypatch.setattr(caller_assistance, 'configured_client', fail)
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_POOL', '1')
    with closing(RuntimeStore(tmp_path)) as store:
        row = _record(text='release process requires pytest before publishing')
        if case == 'cross_scope':
            row.scope = deepcopy(row.scope)
            row.scope.workspace_id = 'elsewhere'
        if case == 'revoked':
            row.status = 'revoked'
        if case == 'source':
            row.source_id = 'forbidden'
        store.append(row)
        source = FakeCandidateSource(() if case == 'empty' else (_hit(row), _hit(row)))
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = None
        memory = MemoryAPI(store, recall_engine=engine)
        bundle = memory.recall(query='release process pytest', scope=asdict(SCOPE),
            task_context={'source_ids': ['alpha'], 'scope_strategy': 'exact'}, limit=1)
        assert [r.record_id for r in bundle.items] == ([row.record_id] if case == 'valid' else [])


@pytest.mark.parametrize('case', ['valid', 'forged', 'missing', 'revoked', 'cross_scope'])
def test_external_bundle_must_match_current_authority(tmp_path, case):
    row = proactive_record('release process requires pytest before publishing')
    runtime, engine, service = _service(tmp_path, [row])
    try:
        candidate = deepcopy(row)
        if case == 'forged':
            candidate.content['text'] = candidate.summary = 'forged release instructions'
        if case == 'missing':
            candidate.record_id = 'nonexistent'
        if case == 'revoked':
            changed = deepcopy(row)
            changed.status = 'revoked'
            runtime.store.append(changed)
        if case == 'cross_scope':
            candidate.scope.workspace_id = 'elsewhere'
        bundle = RecallBundle([candidate], [], [], 1, '', {})
        decision = service.decide(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
            session_id='quality', query_id='one', query='release process pytest', recall_bundle=bundle)
        assert bool(decision['items']) == (case == 'valid')
        assert 'verified-parent-span' not in str(decision)
    finally:
        runtime.close()


def test_raw_recall_does_not_invoke_model_quality(tmp_path):
    from eimemory.raw.retrieval import search_raw_chunks
    from eimemory.models.records import RecordEnvelope
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('quality unavailable')
    with closing(RuntimeStore(tmp_path)) as store:
        store.append(RecordEnvelope.create(kind='raw_chunk', title='release pytest',
            summary='release pytest instructions', content={'raw_text': 'release pytest instructions'},
            scope=SCOPE, source='raw', source_id='alpha'))
        results = search_raw_chunks(store, query='release pytest', scope=SCOPE,
            source_ids=['alpha'], task_context={'llm_reranker': fail}, limit=1)
        assert results and not calls


@pytest.mark.parametrize('case', ['valid', 'forged', 'revoked', 'missing'])
def test_mandatory_fallback_still_requires_real_authorized_record(tmp_path, case):
    row = proactive_record('Never deploy without a receipt')
    row.kind = 'rule'
    runtime, _, service = _service(tmp_path, [row])
    try:
        candidate = deepcopy(row)
        if case == 'forged':
            candidate.summary = 'forged mandatory policy'
        elif case == 'missing':
            candidate.record_id = 'missing'
        elif case == 'revoked':
            changed = deepcopy(row)
            changed.status = 'revoked'
            runtime.store.append(changed)
        result = service.mandatory_fallback(channel='codex', scope=BASE_SCOPE,
            source_ids=['alpha'], records=[candidate], query_id='fallback')
        assert bool(result['items']) == (case == 'valid')
    finally:
        runtime.close()


@pytest.mark.parametrize('failure', [TimeoutError, RuntimeError])
def test_real_engine_proactive_delivery_and_session_dedupe(tmp_path, monkeypatch, failure):
    from eimemory.api.runtime import Runtime
    from eimemory.retrieval.proactive import ProactiveRecallService
    from test_proactive_recall import RELEASE
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise failure('quality unavailable')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_POOL', '1')
    monkeypatch.setattr(caller_assistance, 'configured_client', fail)
    store = RuntimeStore(tmp_path)
    row = store.append(proactive_record('release process requires pytest before publishing'))
    engine = GovernedRecallEngine(store=store, candidate_source=FakeCandidateSource((_hit(row), _hit(row))))
    engine.relevance_admission = None
    runtime = Runtime(store, recall_engine=engine)
    service = ProactiveRecallService(runtime, release_identity=RELEASE, control_percent=0)
    try:
        request = dict(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
            session_id='real-engine', query='release process pytest', task_type='memory.recall')
        first = service.decide(**request, query_id='one')
        assert [r['record_id'] for r in first['items']] == [row.record_id]
        assert len(first['context']) <= service.max_context_chars
        assert 'verified-parent-span' not in str(first)
        second = service.decide(**request, query_id='two')
        assert not second['items'] and not second['context']
        assert not calls
    finally:
        runtime.close()
