from dataclasses import replace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.incremental_sync import SnapshotProjectionReader, delta_snapshot, maintain_memory_projection
from eimemory.retrieval.postgres_sync import SQLiteProjectionReader, ProjectionCursor
from eimemory.retrieval.postgres_vector import IndexState, PostgresVectorConfig, PROJECTION_DIGEST_SCHEMA, projection_fingerprint


def append(runtime, title):
    item = RecordEnvelope.create(kind='memory',title=title,summary=title,scope=ScopeRef(user_id='owner'))
    runtime.store.append(item)
    return item


def test_snapshot_is_durable_and_concurrent_writes_do_not_restart_it(tmp_path):
    runtime = Runtime.create(root=tmp_path / 'authority')
    try:
        reader = SQLiteProjectionReader(runtime.store,projection_memory_only=True)
        first = append(runtime,'first')
        path = tmp_path / 'derived' / 'snapshot.sqlite'
        snapshot = SnapshotProjectionReader(reader,path=path,fingerprint='contract')
        revision = snapshot.snapshot_token()
        snapshot.close()
        append(runtime,'second')
        resumed = SnapshotProjectionReader(reader,path=path,fingerprint='contract')
        assert resumed.snapshot_token() == revision != reader.snapshot_token()
        assert [r['record_id'] for r in resumed.page(ProjectionCursor(),limit=10)] == [first.record_id]
        resumed.close()
        with pytest.raises(ValueError,match='contract_mismatch'):
            SnapshotProjectionReader(reader,path=path,fingerprint='different')
    finally:
        runtime.close()


def test_journal_coalesces_updates_tracks_deletion_and_alias_change(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        reader = SQLiteProjectionReader(runtime.store,projection_memory_only=True)
        since = reader.snapshot_token()
        item = append(runtime,'first')
        row = reader.page(ProjectionCursor(),limit=10)[0]
        conn = runtime.store.sqlite.conn
        for n in range(3):
            conn.execute('UPDATE records SET summary=? WHERE storage_key=?',(str(n),row['storage_key']))
        conn.commit()
        delta = delta_snapshot(reader,since=since,limit=4)
        assert delta['keys'] == [row['storage_key']]
        assert delta['rows'][0]['record_id'] == item.record_id
        before = delta['revision']
        conn.execute('DELETE FROM records WHERE storage_key=?',(row['storage_key'],))
        conn.commit()
        delta = delta_snapshot(reader,since=before,limit=4)
        assert delta['keys'] == [row['storage_key']] and delta['rows'] == []
        with pytest.raises(ValueError,match='delta_journal_gap'):
            delta_snapshot(reader,since='-1',limit=4)
    finally:
        runtime.close()


class Provider:
    max_batch = 2
    def __init__(self):
        self.calls = []
        self.callback = None
    def fingerprint(self): return 'e' * 64
    def embed(self,texts,**kwargs):
        self.calls.append(texts)
        if self.callback:
            callback,self.callback = self.callback,None
            callback()
        return [(0.1,0.2,0.3) for _ in texts]


class Repository:
    def __init__(self,state):
        self.state,self.rows = state,{}
    def read_index_state(self): return self.state
    def reusable_embeddings(self,**kwargs): return {}
    def apply_memory_delta(self,**kwargs):
        assert kwargs['expected_state'] == self.state
        for key in kwargs['changed_keys']:
            self.rows.pop(key,None)
        self.rows.update({p['storage_key']:p for p in kwargs['projections']})
        self.state = replace(self.state,authority_revision=kwargs['authority_revision'])


def test_delta_commits_progress_then_catches_concurrent_write_without_full_scan(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        reader = SQLiteProjectionReader(runtime.store,projection_memory_only=True)
        provider = Provider()
        config = PostgresVectorConfig(enabled=True,dsn='unused',embedding_provider=provider,
            vector_dimension=3,projection_memory_only=True)
        repo = Repository(IndexState(ready=True,watermark='committed',authority_revision=reader.snapshot_token(),
            embedding_fingerprint=provider.fingerprint(),projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(config)))
        first = append(runtime,'first')
        provider.callback = lambda: append(runtime,'concurrent')
        result = maintain_memory_projection(store=runtime.store,repository=repo,config=config,max_pages=1)
        assert result['ok'] and not result['caught_up'] and result['embedded'] == 1
        assert [p['record_id'] for p in repo.rows.values()] == [first.record_id]
        result = maintain_memory_projection(store=runtime.store,repository=repo,config=config,max_pages=1)
        assert result['caught_up'] and result['embedded'] == 1 and len(repo.rows) == 2
        row = reader.page(ProjectionCursor(),limit=10)[0]
        runtime.store.sqlite.conn.execute('DELETE FROM records WHERE storage_key=?',(row['storage_key'],))
        runtime.store.sqlite.conn.commit()
        result = maintain_memory_projection(store=runtime.store,repository=repo,config=config,max_pages=1)
        assert result['caught_up'] and result['embedded'] == 0 and len(repo.rows) == 1
    finally:
        runtime.close()


def test_delta_prefix_does_not_skip_remaining_changes(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        reader = SQLiteProjectionReader(runtime.store,projection_memory_only=True)
        revision = reader.snapshot_token()
        for n in range(7): append(runtime,str(n))
        keys = []
        for _ in range(10):
            result = delta_snapshot(reader,since=revision,limit=2)
            keys.extend(result['keys'])
            revision = result['revision']
            if revision == result['current_revision']: break
        assert len(keys) == len(set(keys)) == 7
    finally:
        runtime.close()
