from contextlib import closing
from dataclasses import asdict, replace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.storage.runtime_store import RuntimeStore


def fragment_source(store, *, before_state_read=lambda request: None):
    """Real source/authority/admission; only embedding IO and SQL rows are synthetic."""
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.retrieval.postgres_vector import (
        IndexState, PostgresVectorConfig, PostgresVectorCandidateSource,
        OpenAICompatibleEmbeddingProvider, projection_fingerprint,
        candidate_record_projection_digest, candidate_record_keyword_text, _canonical_timestamp,
    )
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    provider = OpenAICompatibleEmbeddingProvider(base_url='https://unused.invalid/v1', api_key='test',
        model='test', dimension=3, transport=lambda **kwargs: b'{"data":[{"embedding":[1,0,0]}]}')
    config = PostgresVectorConfig(enabled=True, dsn='postgresql://unused', vector_dimension=3,
                                 evidence_fragments=True)
    sqlite = SQLiteCandidateSource(store)
    head = sqlite.authority_head()
    state = IndexState(ready=True, watermark='synthetic-wm', lag_seconds=0,
        authoritative_updated_at=_canonical_timestamp(head[0]), authoritative_storage_key=head[1],
        authority_revision=sqlite.authority_revision(), embedding_fingerprint=provider.fingerprint(),
        projection_digest_schema='candidate-projection.v1', projection_fingerprint=projection_fingerprint(config))

    class Repository:
        def __init__(self):
            self.reads = 0
            self.requests = []

        def read_index_state(self, **kwargs):
            self.reads += 1
            before_state_read(self)
            return state

        def search(self, request, vector, **kwargs):
            self.requests.append(request)
            rows = []
            for item in store.list_records(kinds=['memory'], scope=request.scope.to_scope_ref(), limit=100):
                if ExactScope.from_scope(item.scope) != request.scope:
                    continue
                text = candidate_record_keyword_text(item, max_text_chars=16000)
                fragment = evidence_fragments(text)[0]
                rows.append(dict(record_id=item.record_id, **asdict(item.scope), source_id=item.source_id,
                    kind=item.kind, status=item.status, vector_score=.9,
                    projection_digest=candidate_record_projection_digest(item, max_text_chars=16000),
                    projection_digest_schema='candidate-projection.v1', index_watermark=state.watermark,
                    authoritative_updated_at=_canonical_timestamp(item.time.updated_at),
                    fragment_id=fragment['id'], fragment_fts_score=.5))
            return rows

    return PostgresVectorCandidateSource(sqlite_source=sqlite, config=config,
        repository=Repository(), embedding_provider=provider)


@pytest.mark.parametrize('query,text,memory_type', [
    ('福建项目的供电方案', '福建项目的供电方案采用专线与市场补电。项目已确定由专用线路提供主要电力，并通过市场采购补足不足部分；这份记录保存方案事实，供后续设计核对使用。', 'fact'),
    ('最近已授权任务、进展、待验收', '最近已授权任务进展：召回修改已完成，目前待验收。', 'conversation'),
], ids=['project_fact', 'task_status'])
@pytest.mark.parametrize('late_scope_failure', ['', 'recall_budget_exhausted', 'index_watermark_changed',
                                             'authority_revision_changed', 'hydration_budget'])
def test_verified_fragments_survive_later_scope_budget(tmp_path, monkeypatch, query, text, memory_type,
                                                      late_scope_failure):
    clock = [10.]
    for module in ('eimemory.retrieval.engine', 'eimemory.retrieval.lightweight_admission',
                   'eimemory.storage.sqlite_store'):
        monkeypatch.setattr(module + '.perf_counter', lambda: clock[0])
    monkeypatch.setattr('eimemory.retrieval.postgres_vector.monotonic', lambda: clock[0])
    monkeypatch.setattr('eimemory.storage.recall_deadline.monotonic', lambda: clock[0])
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    alternate = replace(scope, user_id='alias')
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes_with_aliases', lambda *a, **k: [scope, alternate])
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes', lambda scope: [scope])
    with closing(RuntimeStore(tmp_path)) as store:
        item = store.append(RecordEnvelope.create(kind='memory', title='Hermes completed turn' if memory_type == 'conversation' else '项目记录',
            summary=text, content={'text': text, 'memory_type': memory_type},
            meta={'memory_type': memory_type}, source='user.capture', scope=scope))
        store.append(RecordEnvelope.create(kind='memory', title='unrelated', summary='other scope', scope=alternate))
        def before_read(repo):
            if late_scope_failure and repo.reads == 3:
                clock[0] = 12.8 if late_scope_failure == 'hydration_budget' else 12.3
                # Collection cutoff 12.25, hydration cutoff 12.75, final 13.0.
                raise RuntimeError('recall_budget_exhausted' if late_scope_failure == 'hydration_budget'
                                   else late_scope_failure)
        source = fragment_source(store, before_state_read=before_read)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        bundle = MemoryAPI(store, recall_engine=engine).recall(query=query, scope=asdict(scope), limit=8,
            task_context={'task_type': 'research.task'})
        expected = [] if late_scope_failure in {'index_watermark_changed', 'authority_revision_changed',
                                               'hydration_budget'} else [item.record_id]
        assert [result.record_id for result in bundle.items] == expected, str(bundle.explanation['relevance_selector'])
        if late_scope_failure == 'hydration_budget':
            assert bundle.explanation['relevance_selector']['status'] == 'unavailable'
        assert {request.recall_filter_dict()['_recall_collection_deadline_monotonic']
                for request in source.repository.requests} == {12.25}
        diagnostics = bundle.to_compact_dict()['recall_diagnostics']
        # Two populated scopes plus the authorized empty user fallback.
        assert diagnostics['source_searches'] == 3
        assert diagnostics['admission_status'] == bundle.explanation['relevance_selector']['status']
        if late_scope_failure == 'recall_budget_exhausted':
            assert diagnostics['source_budget_exhausted'] == 1
        if memory_type == 'conversation':
            assert bundle.to_compact_dict()['task_evidence_scope'] == 'historical_only_latest_state_unverified'


def test_sqlite_stops_scoring_at_collection_deadline(tmp_path, monkeypatch):
    from eimemory.storage import sqlite_store
    clock = [1.0]
    # No production-time sleep: model the expensive lexical stage only.
    monkeypatch.setattr(sqlite_store, 'perf_counter', lambda: clock[0], raising=False)
    monkeypatch.setattr('eimemory.storage.recall_deadline.monotonic', lambda: clock[0])
    original = sqlite_store.analyze_lexical_signal
    calls = []
    def timed_lexical(*args, **kwargs):
        calls.append(1)
        clock[0] += .3
        return original(*args, **kwargs)
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        for index in range(8):
            store.append(RecordEnvelope.create(kind='memory', title=f'alpha item {index}',
                summary='alpha distinct facts', content={'text': f'alpha fact {index}'}, scope=scope))
        monkeypatch.setattr(sqlite_store, 'analyze_lexical_signal', timed_lexical)
        items, report = store.search_with_diagnostics(query='alpha', kinds=['memory'], scope=scope, limit=8,
            recall_filters={'_exact_scope': True, '_recall_collection_deadline_monotonic': 1.5})
        assert len(calls) == 2
        assert len(items) == 2
        assert report['blocked_counts']['candidate_scoring_timeout'] == 6


@pytest.mark.parametrize('mode', ['', 'structured', 'fast'])
def test_collection_cutoff_keeps_time_to_validate_collected_identity(tmp_path, monkeypatch, mode):
    clock = [1.0]
    monkeypatch.setattr('eimemory.retrieval.engine.perf_counter', lambda: clock[0])
    monkeypatch.setattr('eimemory.retrieval.lightweight_admission.perf_counter', lambda: clock[0])
    monkeypatch.setattr('eimemory.storage.recall_deadline.monotonic', lambda: clock[0])
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    alternate = replace(scope, user_id='alias')
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes_with_aliases', lambda *a, **k: [scope, alternate])
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes', lambda scope: [scope])
    with closing(RuntimeStore(tmp_path)) as store:
        item = store.append(RecordEnvelope.create(kind='memory', title='specific durable fact',
            summary='specific durable fact', content={'text': 'specific durable fact'}, scope=scope))

        class SlowSource:
            name = 'bounded_test_source'
            calls = 0

            def search(self, request):
                self.calls += 1
                clock[0] += 2.4
                return CandidateBatch(hits=(CandidateHit(
                    CandidateRef(item.record_id, ExactScope.from_scope(scope), item.source_id),
                    1, 1.0, evidence_hints=('exact_title',)),))

        source = SlowSource()
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        memory = MemoryAPI(store, recall_engine=engine)
        monkeypatch.setattr(memory, '_memory_usage_adjustments',
            lambda *a, **k: pytest.fail('optional feedback used validation reserve'))
        bundle = memory.recall(query=item.title, scope=asdict(scope), limit=2,
            task_context={'recall_mode': mode})
        assert source.calls == 1
        assert [result.record_id for result in bundle.items] == [item.record_id]
        assert bundle.explanation['engine_diagnostics']['drops']['recall_budget_exhausted'] >= 1


@pytest.mark.parametrize('query,text,memory_type', [
    ('福建项目的供电方案', '福建项目的供电方案采用专线与市场补电。项目已确定由专用线路提供主要电力，并通过市场采购补足不足部分，供后续设计核对。', 'fact'),
    ('最近已授权任务、进展、待验收', '最近已授权任务进展：召回修改已完成，目前待验收。', 'conversation'),
])
def test_mandatory_fragments_do_not_wait_for_full_sqlite_hybrid(tmp_path, monkeypatch, query, text, memory_type):
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        item = store.append(RecordEnvelope.create(kind='memory', title='durable evidence',
            summary=text, content={'text': text, 'memory_type': memory_type},
            meta={'memory_type': memory_type}, source='user.capture', scope=scope))
        source = fragment_source(store)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        monkeypatch.setattr(store, 'search_with_diagnostics',
            lambda **kwargs: pytest.fail('full local hybrid scorer consumed mandatory fragment budget'))
        bundle = MemoryAPI(store, recall_engine=engine).recall(query=query, scope=asdict(scope), limit=6,
            task_context={'task_type': 'research.task', 'exact_scope_only': True})
        assert [record.record_id for record in bundle.items] == [item.record_id]
        assert bundle.to_compact_dict()['retrieval_status'] == 'evidence_found'
        assert source.repository.requests


def test_identity_only_source_keeps_authority_and_avoids_hybrid(tmp_path, monkeypatch):
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.retrieval.contracts import CandidateRequest
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    other = replace(scope, user_id='other')
    with closing(RuntimeStore(tmp_path)) as store:
        accepted = {'quality': {'capture_decision': 'accept', 'salience_score': .8}}
        expected = store.append(RecordEnvelope.create(kind='memory', title='exact task', scope=scope,
            summary='exact task records a verified delivery result for subsequent reference.', meta=accepted))
        store.append(RecordEnvelope.create(kind='memory', title='exact task', scope=other,
            summary='exact task belongs to the other owner and must remain inaccessible.', meta=accepted))
        store.append(RecordEnvelope.create(kind='memory', title='exact task', scope=scope,
            meta={'quality': {'capture_decision': 'reject'}}))
        source = SQLiteCandidateSource(store)
        monkeypatch.setattr(store, 'search_with_diagnostics',
            lambda **kwargs: pytest.fail('identity lookup must not run hybrid search'))
        request = CandidateRequest.create(query='exact task', scope=scope, kinds=['memory'], limit=6)
        batch = source.search_identity(request)
        assert [hit.ref.record_id for hit in batch.hits] == [expected.record_id]
        assert all(hit.evidence_hints == ('exact_title',) for hit in batch.hits)
        assert source.search_identity(replace(request, query='absent task')).hits == ()


def test_authority_probe_does_not_wait_past_collection_deadline(tmp_path):
    import threading
    from time import monotonic
    from eimemory.retrieval.contracts import CandidateRequest
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        store.append(RecordEnvelope.create(kind='memory', title='populated scope', scope=scope))
        source = fragment_source(store)
        acquired, release = threading.Event(), threading.Event()
        def hold_lock():
            with store._lock:
                acquired.set()
                release.wait(1)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        assert acquired.wait(1)
        try:
            started = monotonic()
            batch = source.search(CandidateRequest.create(query='facts', scope=scope, kinds=['memory'], limit=6,
                recall_filters={'_recall_collection_deadline_monotonic': started + .03}))
            assert monotonic() - started < .5
            assert batch.diagnostic_dict()['postgres']['error_code'] == 'recall_budget_exhausted'
            assert source.repository.reads == 0
            assert source.health()['circuit'] == 'closed'
        finally:
            release.set()
            thread.join(2)


@pytest.mark.parametrize('read_number', [1, 2], ids=['initial_authority', 'authority_recheck'])
def test_authority_reads_after_remote_io_respect_deadline(tmp_path, read_number):
    import threading
    from time import monotonic
    from eimemory.retrieval.contracts import CandidateRequest
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        store.append(RecordEnvelope.create(kind='memory', title='populated scope', scope=scope))
        acquired, release = threading.Event(), threading.Event()
        def hold_lock():
            with store._lock:
                acquired.set()
                release.wait(1)
        thread = threading.Thread(target=hold_lock)
        def before_read(repo):
            if repo.reads == read_number:
                thread.start()
                assert acquired.wait(1)
        source = fragment_source(store, before_state_read=before_read)
        try:
            started = monotonic()
            batch = source.search(CandidateRequest.create(query='facts', scope=scope, kinds=['memory'], limit=6,
                recall_filters={'_recall_collection_deadline_monotonic': started + .1,
                                '_require_fragment_evidence': True}))
            assert monotonic() - started < .5
            assert batch.diagnostic_dict()['postgres']['error_code'] == 'recall_budget_exhausted'
            assert source.repository.reads == read_number
            assert source.health()['circuit'] == 'closed'
        finally:
            release.set()
            thread.join(2)


@pytest.mark.parametrize('phase', ['canonical_probe', 'hydration', 'admission_identity', 'final_validation'])
def test_engine_local_authority_reads_respect_phase_deadline(tmp_path, monkeypatch, phase):
    import threading
    from time import monotonic
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        store.append(RecordEnvelope.create(kind='memory', title='project evidence', scope=scope,
            summary='福建项目的供电方案采用专线与市场补电。项目已确定由专用线路提供主要电力，并通过市场采购补足不足部分，供后续设计核对。',
            meta={'memory_type': 'fact'}))
        source = fragment_source(store)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        memory = MemoryAPI(store, recall_engine=engine)
        acquired, release = threading.Event(), threading.Event()
        def hold_lock():
            with store._lock:
                acquired.set()
                release.wait(1)
        thread = threading.Thread(target=hold_lock)
        def start_holder():
            if not thread.ident:
                thread.start()
                assert acquired.wait(1)
        if phase in {'hydration', 'canonical_probe'}:
            original = source.search
            def blocked(*args, **kwargs):
                result = original(*args, **kwargs)
                assert result.hits
                start_holder()
                return result
            monkeypatch.setattr(source, 'search', blocked)
        else:
            method = '_select_post_fusion_items' if phase == 'admission_identity' else '_record_is_unchanged'
            original = getattr(engine, method)
            def blocked(*args, **kwargs):
                start_holder()
                return original(*args, **kwargs)
            monkeypatch.setattr(engine, method, blocked)
        context = {'task_type': 'research.task', 'exact_scope_only': True}
        if phase == 'canonical_probe':
            context = {'task_type': 'research.task', 'scope_strategy': 'canonical_first'}
            monkeypatch.setattr('eimemory.retrieval.engine.is_hongtuish_scope', lambda *a, **k: True)
            monkeypatch.setattr('eimemory.retrieval.engine.hongtu_scope', lambda *a, **k: asdict(scope))
            monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes_with_aliases',
                lambda *a, **k: [scope, replace(scope, user_id='alias')])
        try:
            started = monotonic()
            bundle = memory.recall(query='福建项目的供电方案', scope=asdict(scope), limit=3,
                task_context={**context, '_recall_deadline_monotonic': started + .2})
            assert thread.ident
            assert monotonic() - started < .6
            assert not bundle.items
            assert bundle.to_compact_dict()['retrieval_status'] == 'unavailable'
            if phase == 'hydration':
                assert bundle.explanation['engine_diagnostics']['drops']['candidate_hydration_timeout'] == 1
            if phase == 'canonical_probe':
                assert bundle.explanation['engine_diagnostics']['drops']['recall_budget_exhausted'] >= 1
        finally:
            release.set()
            if thread.ident:
                thread.join(2)
        assert store.sqlite.conn.execute('SELECT 1').fetchone()[0] == 1
