from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_unicode61_upgrade_commits_each_batch_and_preserves_fts(tmp_path):
    path = tmp_path / 'upgrade.sqlite'
    store = SqliteRecordStore(path)
    record = RecordEnvelope.create(kind='knowledge_page', title='部署迁移验证', content={'text': 'transaction canary'}, scope=ScopeRef(tenant_id='default', agent_id='agent', workspace_id='workspace', user_id=''))
    import threading
    lock = threading.RLock()
    store.bind_runtime_lock(lock)
    with lock:
        store.upsert(record)
    with store.conn:
        store.conn.execute('DROP TABLE recall_index_fts')
        store.conn.execute("CREATE VIRTUAL TABLE recall_index_fts USING fts5(storage_key UNINDEXED,title_text,body_text,anchor_terms,tokenize='unicode61')")
        store.conn.execute('INSERT INTO recall_index_fts SELECT storage_key,title_text,body_text,anchor_terms FROM recall_index')
        store.conn.execute("DELETE FROM schema_migrations WHERE migration_id='recall.fts_trigram.v1'")
    store.close()
    store = SqliteRecordStore(path)
    try:
        assert 'recall.fts_trigram.v1' in store.pending_storage_migrations()
        for _ in range(20):
            report = store.apply_storage_migrations(batch_size=1, offline=True)
            assert not store.conn.in_transaction, 'maintenance batch leaked a transaction'
            if not report['pending']:
                break
        assert not report['pending']
        assert store._fts_tokenizer_in_use() == 'trigram'
        assert store.conn.execute("SELECT count(*) FROM recall_index_fts WHERE recall_index_fts MATCH 'transaction'").fetchone()[0] == 1
    finally:
        store.close()
