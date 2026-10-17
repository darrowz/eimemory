"""Sparse exact partitions must not scan unrelated global FTS matches."""
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.evaluation.controlled_memory_benchmark import _case, _run_case
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage import recall_deadline
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef('synthetic', 'sparse-fts', 'isolated', 'owner')
QUERY = ' '.join(f'orchid{n:02d}' for n in range(32))


def put(store, n, *, scope=SCOPE, source='synthetic', text=QUERY):
    record = RecordEnvelope.create(kind='memory', title=text,
        summary=text, content={'text': text, 'memory_type': 'fact'},
        scope=scope, source_id=source, source='test.sparse',
        meta={'memory_type': 'fact', 'force_capture': True,
              'quality': {'capture_decision': 'accept', 'salience_score': .8}})
    record.record_id = f'mem_sparse_{n:04d}'
    record.time.updated_at = '2026-01-01T00:00:00Z'
    return store.append(record)


def add_foreign_projection_noise(store, template, count=4096):
    """Only foreign candidate projections are bulk-generated; authority stays real."""
    with store._lock:
        sqlite = store.sqlite
        row = dict(sqlite.conn.execute('SELECT * FROM recall_index WHERE storage_key=?',
            (sqlite._storage_key(template),)).fetchone())
        rows = []
        for n in range(count):
            rows.append({**row, 'storage_key': f'foreign-{n}', 'record_id': f'mem_foreign_{n}',
                         'workspace_id': 'foreign'})
        columns = list(row)
        sqlite.conn.executemany('INSERT INTO recall_index (' + ','.join(columns) + ') VALUES (' +
            ','.join('?' for _ in columns) + ')', [[r[k] for k in columns] for r in rows])
        sqlite._fts_insert([(r['storage_key'],r['title_text'],r['body_text'],r['anchor_terms']) for r in rows])
        sqlite.conn.commit()


@pytest.mark.parametrize('guard', ['complete', 'hard_deadline', 'authority_changed'])
@pytest.mark.parametrize('query', [QUERY, 'Hermes deployment replay'])
def test_slow_large_foreign_fts_preserves_completeness_and_authority(tmp_path, monkeypatch, guard, query):
    clock = [100.0]
    for module in ('eimemory.api.memory', 'eimemory.retrieval.engine',
                   'eimemory.retrieval.authority_gate', 'eimemory.retrieval.verification_budget',
                   'eimemory.retrieval.sqlite_source', 'eimemory.storage.runtime_store',
                   'eimemory.storage.sqlite_store'):
        monkeypatch.setattr(module + '.perf_counter', lambda: clock[0])
    monkeypatch.setattr(recall_deadline, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('EIMEMORY_RECALL_BUDGET_SECONDS', '3')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED', '0')
    with closing(RuntimeStore(tmp_path)) as store:
        target = put(store, 0, text=query)
        for n in range(1, 527 if query != QUERY else 55):
            put(store, n, text=f'unrelated synthetic filler {n}')
        add_foreign_projection_noise(store, target)
        # Charge the same deterministic latency for each FTS key lookup.
        # The function returns its input; SQL matches and bm25 remain native.
        store._ensure_readers()
        visits = []
        def slow_key(key):
            visits.append(key)
            clock[0] += .001
            return key
        for owner in (store.sqlite, *(slot.store for slot in store._readers)):
            owner.conn.create_function('synthetic_key_latency', 1, slow_key)
        original = recall_deadline._ReadConnection.execute
        def execute(connection, sql, parameters=()):
            if 'bm25(recall_index_fts)' in sql:
                sql = sql.replace('recall_index_fts.storage_key',
                    'synthetic_key_latency(recall_index_fts.storage_key)')
            return original(connection, sql, parameters)
        monkeypatch.setattr(recall_deadline._ReadConnection, 'execute', execute)
        source = SQLiteCandidateSource(store)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = None
        guarded = []
        if guard == 'hard_deadline':
            search = source.search
            def late_source(request):
                batch = search(request)
                assert batch.hits
                guarded.append(guard)
                clock[0] = 103.001
                return batch
            monkeypatch.setattr(source, 'search', late_source)
        elif guard == 'authority_changed':
            select = engine._select_post_fusion_items
            def changed_source(items, **kwargs):
                assert items
                guarded.append(guard)
                store.append(replace(target, summary='changed authoritative synthetic fact',
                    content={**target.content, 'text': 'changed authoritative synthetic fact'}))
                return select(items, **kwargs)
            monkeypatch.setattr(engine, '_select_post_fusion_items', changed_source)
        runtime = SimpleNamespace(store=store, memory=MemoryAPI(store, recall_engine=engine))
        sample = _run_case(runtime, _case(target), SCOPE, ('synthetic',))
        assert visits, 'native FTS SQL must reach the controlled slow boundary'
        if guard != 'complete':
            assert guarded == [guard]
            assert not sample['evaluable'] and not sample['passed']
            assert sample['failure_reason'] == 'controlled_retrieval_incomplete'
            return
        assert sample['evaluable'], sample.get('failure_reason')
        assert sample['returned_record_count'] >= 1
        assert sample['passed'] and sample['expected_rank'] == 1
        assert clock[0] < 102.25
        assert not any(key.startswith('foreign-') for key in visits)


@pytest.mark.parametrize('damage', ['', 'missing_table', 'missing_key', 'wrong_key', 'large_partition', 'interleaved'])
@pytest.mark.parametrize('rank_offset', [0., 36.])
def test_fts_exactly_matches_original_bm25_order_and_partition(tmp_path, damage, rank_offset):
    with closing(RuntimeStore(tmp_path)) as store:
        target = put(store, 0)
        for n in range(1, 36):
            put(store, n, text=QUERY if n % 3 else 'orchid00 orchid31 near negative')
            if damage == 'interleaved':
                put(store, 300 + n, scope=replace(SCOPE, user_id='foreign'))
                put(store, 400 + n, source='other')
        put(store, 200, scope=replace(SCOPE, user_id='foreign'))
        put(store, 201, source='other')
        if damage == 'large_partition':
            add_foreign_projection_noise(store, target, count=1025)
            with store._lock:
                store.sqlite.conn.execute("UPDATE recall_index SET workspace_id=? WHERE workspace_id='foreign'", (SCOPE.workspace_id,))
                store.sqlite.conn.commit()
        with store._lock:
            sqlite = store.sqlite
            if damage == 'missing_table':
                sqlite.conn.execute('DROP TABLE recall_index_fts_keys')
            elif damage == 'missing_key':
                sqlite.conn.execute('DELETE FROM recall_index_fts_keys WHERE storage_key=?', (sqlite._storage_key(target),))
            elif damage == 'wrong_key':
                sqlite.conn.execute('UPDATE recall_index_fts_keys SET fts_rowid=99999999 WHERE storage_key=?', (sqlite._storage_key(target),))
            filters = {'_exact_scope': True, '_source_ids': ('synthetic',)}
            where, params = sqlite._recall_index_where(kinds=['memory'], scope=SCOPE, recall_filters=filters, alias='i')
            where.append("i.status = 'active'")
            expression = sqlite._long_query_fts_query(QUERY) if rank_offset else sqlite._fts_query(QUERY)
            baseline_sql = ('SELECT i.storage_key FROM recall_index_fts JOIN recall_index i '
                'ON i.storage_key = recall_index_fts.storage_key WHERE recall_index_fts MATCH ? AND ' +
                ' AND '.join(where) + ' ORDER BY bm25(recall_index_fts) ASC, i.quality_score DESC, i.updated_at DESC LIMIT ?')
            expected = [str(r[0]) for r in sqlite.conn.execute(baseline_sql, [expression,*params,16])]
            selected = {}
            statements = []
            sqlite.conn.set_trace_callback(statements.append)
            try:
                sqlite._collect_fts_candidates(selected, fts_query=expression, kinds=['memory'], scope=SCOPE,
                    limit=16, recall_filters=filters, rank_offset=rank_offset)
            finally:
                sqlite.conn.set_trace_callback(None)
            match_sql = next(sql for sql in statements if 'bm25(recall_index_fts)' in sql)
            assert ('recall_index_fts.rowid >=' in match_sql) is (damage in ('', 'interleaved'))
            assert list(selected) == expected
            assert [v['rank'] for v in selected.values()] == list(range(int(10 + rank_offset), int(26 + rank_offset)))
            assert all(v['sources'] == ['fts'] for v in selected.values())
            assert len(expected) == 16


def test_many_local_matches_do_not_repeat_global_fts_work(tmp_path):
    """A sparse scope can match every row; MATCH setup must stay bounded."""
    with closing(RuntimeStore(tmp_path)) as store:
        for n in range(55):
            target = put(store, n)
        add_foreign_projection_noise(store, target, count=256)
        sqlite = store.sqlite
        work = {}
        results = {}
        for name in ('original', 'optimized'):
            if name == 'original':
                sqlite._fts_key_map_ready = lambda: False
            else:
                del sqlite._fts_key_map_ready
            instructions = [0]
            def progress():
                instructions[0] += 100
                return 0
            sqlite.conn.set_progress_handler(progress, 100)
            try:
                selected = {}
                sqlite._collect_fts_candidates(selected,
                    fts_query=sqlite._long_query_fts_query(QUERY), kinds=['memory'],
                    scope=SCOPE, limit=16,
                    recall_filters={'_exact_scope': True, '_source_ids': ('synthetic',)},
                    rank_offset=36.)
                results[name] = selected
                work[name] = instructions[0]
            finally:
                sqlite.conn.set_progress_handler(None, 0)
        assert results['optimized'] == results['original']
        assert work['optimized'] <= 2 * work['original'], work
