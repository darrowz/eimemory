"""Current backend readiness must never resurrect a caller's invalid proof."""
from dataclasses import replace
from contextlib import contextmanager
import threading
import time

import pytest

from eimemory.retrieval.contracts import CandidateBatch, CandidateRequest, ExactScope
from eimemory.retrieval.postgres_vector import (
    IndexState, PostgresVectorCandidateSource, PostgresVectorConfig,
    PROJECTION_DIGEST_SCHEMA, projection_fingerprint,
)


class Authority:
    head = ('2026-10-18T00:00:00.000000Z', 'physical-head')
    revision = '7'
    calls = 0

    def authority_snapshot(self, *, deadline_at=0.0):
        assert deadline_at > 0
        self.calls += 1
        return self.head, self.revision

    def authority_head(self): return self.head
    def authority_revision(self): return self.revision
    def search(self, request): return CandidateBatch(hits=())


class Provider:
    calls = 0
    available = True
    def embed(self, texts, **kwargs):
        self.calls += 1
        return [(1.0, 0.0, 0.0)]
    def fingerprint(self): return 'f' * 64
    def health(self):
        return {'configured': True, 'available': self.available, 'circuit': 'closed',
                'dimension': 3, 'last_error': ''}


class Repository:
    def __init__(self, config, authority):
        self.state = IndexState(ready=True, watermark='watermark-7', lag_seconds=0.0,
            authoritative_updated_at=authority.head[0], authoritative_storage_key=authority.head[1],
            embedding_fingerprint='f' * 64, projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(config), authority_revision=authority.revision)
        self.reads = []; self.searches = []; self.error = None; self.on_search = None
    def read_index_state(self, *, deadline_at=0.0):
        self.reads.append(deadline_at)
        return self.state
    def search(self, request, vector, *, top_k, watermark):
        self.searches.append((request, vector, top_k, watermark))
        if self.on_search: self.on_search()
        if self.error: raise self.error
        return []


def setup_source(**overrides):
    config = PostgresVectorConfig(enabled=True, dsn='postgresql://fixture-not-connected',
        vector_dimension=3, **overrides)
    authority = Authority(); provider = Provider(); repository = Repository(config, authority)
    source = PostgresVectorCandidateSource(sqlite_source=authority, config=config,
        repository=repository, embedding_provider=provider)
    return source, authority, provider, repository


def test_authority_sync_recovers_with_real_vector_select_without_resurrecting_old_query():
    source, authority, provider, repository = setup_source()
    repository.state = replace(repository.state, authority_revision='6')
    source.search(CandidateRequest(query='actual authorized retrieval', scope=ExactScope('t', 'a', 'w', 'u')))
    assert source.health()['last_error'] == 'authority_revision_changed'
    assert source._circuit.failures == 0
    invalid = (source._last_query_valid, source._last_query_index_identity, source._backend_query_identity,
               source._last_query_status, source._last_error)
    assert not source.readiness(force=True)['ready']
    assert repository.searches == [] and provider.calls == 0
    repository.state = replace(repository.state, authority_revision='7')
    ready = source.readiness(force=True)
    assert ready['schema'] == 'postgres_candidate_readiness.v1'
    assert ready['measurement_origin'] == 'native_readonly_vector_query'
    assert all(ready[key] is True for key in ('ready', 'enabled', 'configured', 'embedding_available',
        'index_verified', 'backend_query_verified', 'authority_unchanged', 'fingerprints_match', 'revision_matches'))
    assert ready['lag_seconds'] == 0.0 and ready['last_error'] == ready['error'] == ''
    assert len(repository.searches) == 1 and provider.calls == 0
    request, vector, top_k, watermark = repository.searches[0]
    assert request.scope == ExactScope(*(['eimemory-readiness-private-v1'] * 4))
    assert request.source_ids == ('eimemory-readiness-private-v1',) and top_k == request.limit == 1
    assert vector == (1.0, 0.0, 0.0) and watermark == 'watermark-7'
    assert all(deadline > 0 for deadline in repository.reads[-2:])
    assert invalid == (source._last_query_valid, source._last_query_index_identity, source._backend_query_identity,
        source._last_query_status, source._last_error)
    assert not source.health()['query_valid'] and not source.health()['available']


@pytest.mark.parametrize('change,code', [
    ({'ready': False}, 'index_not_ready'), ({'watermark': ''}, 'index_not_ready'),
    ({'authority_revision': '8'}, 'authority_revision_changed'),
    ({'embedding_fingerprint': 'e' * 64}, 'embedding_fingerprint_mismatch'),
    ({'projection_fingerprint': 'e' * 64}, 'projection_fingerprint_mismatch'),
    ({'projection_digest_schema': 'unrecognized'}, 'projection_fingerprint_mismatch'),
])
def test_stale_or_wrong_index_cannot_pass_by_metadata_or_query_cache(change, code):
    source, authority, provider, repository = setup_source()
    repository.state = replace(repository.state, **change)
    result = source.readiness(force=True)
    assert not result['ready'] and result['last_error'] == code
    assert not result['backend_query_verified'] and not result['authority_unchanged']
    assert repository.searches == [] and provider.calls == 0 and source._circuit.failures == 0


@pytest.mark.parametrize('side', ['sqlite', 'revision', 'watermark', 'head', 'fingerprint'])
def test_actual_vector_success_requires_stable_sqlite_and_pg_fences(side):
    source, authority, provider, repository = setup_source()
    def change():
        if side == 'sqlite': authority.revision = '8'
        if side == 'revision': repository.state = replace(repository.state, authority_revision='8')
        if side == 'watermark': repository.state = replace(repository.state, watermark='next-watermark')
        if side == 'head': repository.state = replace(repository.state, authoritative_storage_key='other-head')
        if side == 'fingerprint': repository.state = replace(repository.state, projection_fingerprint='0' * 64)
    repository.on_search = change
    result = source.readiness(force=True)
    assert not result['ready'] and not result['authority_unchanged']
    assert len(repository.searches) == 1 and source._circuit.failures == 0


def test_metadata_refresh_cannot_clear_real_backend_failure_but_new_actual_query_can():
    source, authority, provider, repository = setup_source(failure_threshold=2, cooldown_seconds=.05)
    source._record_query_failure('postgres_timeout')
    assert source._circuit.failures == 1
    assert source.refresh_index_identity(force=True)
    assert source._circuit.failures == 1 and not source.health()['query_valid']
    repository.error = TimeoutError('private backend details must never be exposed')
    failed = source.readiness(force=True)
    assert failed['last_error'] == 'postgres_timeout' and not failed['ready']
    assert source._circuit.state() == 'open'
    reads = len(repository.reads)
    assert source.readiness(force=True)['last_error'] == 'circuit_open'
    assert len(repository.reads) == reads
    # Deterministically expire cooldown without a test sleep.
    source._circuit.opened_at -= 1
    assert source.refresh_index_identity(force=True)
    assert source._circuit.state() == 'half_open' and source._circuit.failures == 2
    repository.error = None
    assert source.readiness(force=True)['ready']
    assert source._circuit.state() == 'closed' and source._circuit.failures == 0
    assert source._last_query_valid is False and source._backend_query_identity is None


def test_readiness_has_independent_ttl_and_real_query_fault_invalidates_cached_success():
    source, authority, provider, repository = setup_source(identity_refresh_ttl_seconds=10)
    clock = [1.0]; source._clock = lambda: clock[0]
    first = source.readiness(); assert first['ready']
    assert source.readiness() == first and len(repository.searches) == 1
    source._record_query_failure('postgres_timeout')
    repository.error = TimeoutError()
    assert not source.readiness()['ready'] and len(repository.searches) == 2
    repository.error = None; clock[0] += 11
    assert source.readiness()['ready'] and len(repository.searches) == 3


def test_positive_ttl_cache_must_recheck_atomic_authority_and_cannot_hide_new_query_failure():
    source, authority, provider, repository = setup_source(identity_refresh_ttl_seconds=60)
    first = source.readiness(); assert first['ready']
    authority.revision = '8'
    repository.state = replace(repository.state, authority_revision='8', watermark='watermark-8')
    second = source.readiness()
    assert second['ready'] and second['authority_revision'] == '8' and len(repository.searches) == 2
    original = authority.authority_snapshot
    def actual_failure_during_cache_fence(**kwargs):
        source._record_query_failure('postgres_timeout')
        return original(**kwargs)
    authority.authority_snapshot = actual_failure_during_cache_fence
    # Each actual query failure changes the generation even if the SQL pair is
    # unchanged.  Neither TTL nor an overlapping probe may erase that failure.
    result = source.readiness()
    assert not result['ready'] and source.health()['last_error'] == 'postgres_timeout'
    assert source._circuit.failures >= 1


def test_readiness_hard_deadline_keeps_single_flight_until_late_worker_finishes():
    source, authority, provider, repository = setup_source(identity_refresh_ttl_seconds=0)
    entered = threading.Event(); release = threading.Event()
    def blocked():
        entered.set(); assert release.wait(2)
    repository.on_search = blocked
    try:
        started = time.monotonic()
        result = source.readiness(timeout_seconds=.05)
        assert entered.is_set() and time.monotonic() - started < .5
        assert not result['ready'] and result['last_error'] == 'postgres_timeout'
        assert source.readiness(force=True)['last_error'] == 'readiness_probe_in_progress'
        assert len(repository.searches) == 1
    finally:
        release.set()
        assert source._readiness_lock.acquire(timeout=1)
        source._readiness_lock.release()
    assert source._readiness_cached['ready'] is False
    assert source._last_query_valid is False and source._backend_query_identity is None
    repository.on_search = None
    assert source.readiness(force=True)['ready']


def test_new_real_failure_during_probe_cannot_be_erased_by_older_success():
    source, authority, provider, repository = setup_source()
    repository.on_search = lambda: source._record_query_failure('postgres_timeout')
    result = source.readiness(force=True)
    assert not result['ready'] and source._circuit.failures == 1
    assert source.health()['last_error'] == 'postgres_timeout' and source._backend_query_identity is None


def test_deadline_expiring_after_submit_keeps_the_running_probe_single_flight(monkeypatch):
    from eimemory.retrieval import postgres_vector as native
    source, authority, provider, repository = setup_source(identity_refresh_ttl_seconds=0)
    entered = threading.Event(); release = threading.Event(); submitted = threading.Event()
    real_executor = native.ThreadPoolExecutor
    real_remaining = native._remaining_timeout
    def blocked():
        entered.set(); assert release.wait(2)
    repository.on_search = blocked
    class Executor(real_executor):
        def submit(self, *args, **kwargs):
            pending = super().submit(*args, **kwargs)
            assert entered.wait(1)
            submitted.set()
            return pending
    def deadline_after_submit(deadline, maximum):
        if threading.current_thread() is threading.main_thread() and submitted.is_set():
            submitted.clear()
            raise RuntimeError('recall_budget_exhausted')
        return real_remaining(deadline, maximum)
    monkeypatch.setattr(native, 'ThreadPoolExecutor', Executor)
    monkeypatch.setattr(native, '_remaining_timeout', deadline_after_submit)
    try:
        report = source.readiness(force=True, timeout_seconds=.2)
        assert report['ready'] is False and report['error'] == 'recall_budget_exhausted'
        assert source.readiness(force=True)['error'] == 'readiness_probe_in_progress'
        assert len(repository.searches) == 1 and provider.calls == 0
    finally:
        release.set()
        assert source._readiness_lock.acquire(timeout=1)
        source._readiness_lock.release()
    assert source._readiness_cached['ready'] is False
    assert source._last_query_valid is False and source._backend_query_identity is None
    repository.on_search = None
    monkeypatch.setattr(native, 'ThreadPoolExecutor', real_executor)
    assert source.readiness(force=True)['ready'] is True


def test_missing_atomic_authority_hook_and_unavailable_provider_fail_closed():
    source, authority, provider, repository = setup_source()
    authority.authority_snapshot = None
    result = source.readiness(force=True)
    assert not result['ready'] and result['last_error'] == 'authority_cursor_unavailable'
    assert repository.reads == [] and source._circuit.failures == 0
    provider.available = False
    assert source.readiness(force=True)['last_error'] == 'embedding_unavailable'
    assert provider.calls == 0


def test_private_query_rows_and_backend_exceptions_never_escape_readiness():
    source, authority, provider, repository = setup_source()
    original = repository.search
    def with_private_row(*args, **kwargs):
        original(*args, **kwargs)
        return [{'record_id': 'do-not-publish-record', 'text': 'private business body'}]
    repository.search = with_private_row
    assert source.readiness(force=True)['ready']
    assert 'private business body' not in str(source._readiness_cached)
    assert 'do-not-publish-record' not in str(source._readiness_cached)
    repository.search = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('postgresql://secret@internal/private'))
    report = source.readiness(force=True)
    assert not report['ready'] and report['last_error'] == 'postgres_unavailable'
    assert 'secret' not in str(report) and 'internal' not in str(report)


@pytest.mark.parametrize('worker_completed', [False, True])
def test_timed_future_wait_budget_does_not_depend_on_platform_clock_rounding(monkeypatch, worker_completed):
    from eimemory.retrieval import postgres_vector as native
    source, authority, provider, repository = setup_source()
    callbacks = []
    failure = TimeoutError('actual backend timeout')
    class Pending:
        def add_done_callback(self, callback):callbacks.append(callback)
        def result(self, **kwargs):raise failure
        def done(self):return worker_completed
        def exception(self, **kwargs):return failure if worker_completed else None
    class Executor:
        def __init__(self, **kwargs):pass
        def submit(self, *args, **kwargs):return Pending()
        def shutdown(self, **kwargs):pass
    monkeypatch.setattr(native, 'ThreadPoolExecutor', Executor)
    # The synthetic wait times out while the actual deadline remains far in
    # the future.  Its provenance is still a request-owned Future wait unless
    # the completed worker itself has raised a real timeout.
    request = CandidateRequest(query='actual authorized retrieval', scope=ExactScope('t','a','w','u'),
        recall_filters=(('_recall_collection_deadline_monotonic', time.monotonic()+10),))
    result = source.search(request)
    code = result.diagnostic_dict()['postgres']['error_code']
    assert code == ('postgres_timeout' if worker_completed else 'recall_budget_exhausted')
    assert source._circuit.failures == (1 if worker_completed else 0)
    for callback in callbacks:callback(None)


def test_sqlite_head_and_revision_use_one_wal_snapshot_even_if_writer_commits_between_selects(tmp_path, monkeypatch):
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.models.records import RecordEnvelope, ScopeRef
    store = RuntimeStore(tmp_path)
    try:
        source = SQLiteCandidateSource(store)
        item = RecordEnvelope.create(kind='memory', title='Existing source', scope=ScopeRef(), content={'text':'Existing business fact.'})
        store.append(item)
        expected = source.authority_snapshot()
        later = RecordEnvelope.create(kind='memory', title='Later source', scope=ScopeRef(), content={'text':'Later business fact.'})
        later.time.updated_at = '2099-01-01T00:00:00Z'
        original = store.borrow_reader; injected = [False]
        @contextmanager
        def borrow(*args, **kwargs):
            with original(*args, **kwargs) as slot:
                execute = slot.store.execute
                def interleaved(sql, parameters=()):
                    cursor = execute(sql, parameters)
                    if sql.startswith('SELECT updated_at,storage_key') and not injected[0]:
                        injected[0] = True
                        store.append(later)
                    return cursor
                slot.store.execute = interleaved
                try: yield slot
                finally: del slot.store.execute
        monkeypatch.setattr(store, 'borrow_reader', borrow)
        assert source.authority_snapshot(deadline_at=time.monotonic()+1) == expected
        assert injected[0]
        monkeypatch.setattr(store, 'borrow_reader', original)
        changed = source.authority_snapshot()
        assert changed[0] != expected[0] and int(changed[1]) > int(expected[1])
    finally: store.close()


def test_memory_only_atomic_pair_ignores_audit_writes_and_deadline_bounds_lock_acquisition(tmp_path):
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded
    store = RuntimeStore(tmp_path)
    try:
        source = SQLiteCandidateSource(store, projection_memory_only=True)
        item = RecordEnvelope.create(kind='memory', title='Existing source', scope=ScopeRef(), content={'text':'Existing business fact.'})
        store.append(item); expected = source.authority_snapshot()
        audit = RecordEnvelope.create(kind='evaluation_packet', title='Operational audit', scope=ScopeRef())
        store.append(audit)
        assert source.authority_snapshot() == expected
        with pytest.raises(RecallReadDeadlineExceeded):
            source.authority_snapshot(deadline_at=time.monotonic()-.001)
    finally: store.close()


def test_memory_head_has_a_covering_keyset_without_sorting_the_projection(tmp_path):
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
    from eimemory.models.records import RecordEnvelope, ScopeRef
    store = RuntimeStore(tmp_path)
    try:
        source = SQLiteCandidateSource(store, projection_memory_only=True)
        for index in range(12):
            item = RecordEnvelope.create(kind='memory', title=f'Historical source {index}',
                scope=ScopeRef(), content={'text': f'Existing business fact {index}.'})
            item.time.updated_at = '2026-10-18T00:00:00.000000Z'
            store.append(item)
        query = "SELECT updated_at,storage_key FROM records WHERE kind='memory' AND status='active' ORDER BY updated_at DESC,storage_key DESC LIMIT 1"
        def inspect(sqlite):
            plan = ' '.join(str(row[3]) for row in sqlite.execute('EXPLAIN QUERY PLAN '+query))
            assert 'idx_records_active_memory_vector_cursor' in plan
            assert 'TEMP B-TREE' not in plan
            steps = [0]
            sqlite.conn.set_progress_handler(lambda: steps.__setitem__(0,steps[0]+1) or 0, 1)
            try:head = tuple(sqlite.execute(query).fetchone())
            finally:sqlite.conn.set_progress_handler(None,0)
            assert steps[0] < 100
            return head
        expected = store.read_consistent(inspect)
        assert source.authority_snapshot()[0] == expected
        before = source.authority_snapshot()
        store.append(RecordEnvelope.create(kind='evaluation_packet', title='Independent operational observation', scope=ScopeRef()))
        assert source.authority_snapshot() == before
    finally:store.close()
