from dataclasses import replace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.postgres_sync import SQLiteProjectionReader, ProjectionCursor
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.retrieval.postgres_vector import PostgresVectorConfig, projection_fingerprint


def test_memory_domain_ignores_audits_but_fences_all_memory_mutations(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        source = SQLiteCandidateSource(runtime.store, projection_memory_only=True)
        reader = SQLiteProjectionReader(runtime.store, projection_memory_only=True)
        token = reader.snapshot_token()
        audit = RecordEnvelope.create(kind='reflection', title='audit', scope=ScopeRef('t', 'a', 'w', 'u'))
        runtime.store.append(audit)
        assert reader.snapshot_token() == token == source.authority_revision()
        assert reader.page(ProjectionCursor(), limit=10) == []
        memory = RecordEnvelope.create(kind='memory', title='memory', aliases=['original'], scope=ScopeRef('t', 'a', 'w', 'u'))
        runtime.store.append(memory)
        assert reader.snapshot_token() != token
        row = reader.page(ProjectionCursor(), limit=10)[0]
        assert row['record_id'] == memory.record_id
        assert source.authority_head() == (row['updated_at'], row['storage_key'])
        conn = runtime.store.sqlite.conn
        for sql in (
            "UPDATE records SET status='archived' WHERE kind='memory'",
            "UPDATE recall_alias_index SET normalized_alias='changed' WHERE storage_key=?",
            "UPDATE records SET kind='reflection' WHERE storage_key=?",
            "UPDATE records SET kind='memory' WHERE storage_key=?",
            "DELETE FROM records WHERE storage_key=?",
        ):
            before = reader.snapshot_token()
            conn.execute(sql, (row['storage_key'],) if '?' in sql else ())
            conn.commit()
            assert reader.snapshot_token() != before
            assert reader.snapshot_token() == source.authority_revision()
        assert reader.page(ProjectionCursor(), limit=10) == []
        assert source.authority_head() == ('', '')
    finally:
        runtime.close()


def test_projection_domain_changes_index_fingerprint():
    config = PostgresVectorConfig()
    assert projection_fingerprint(config) != projection_fingerprint(replace(config, projection_memory_only=True))


def test_existing_attachment_uses_real_memory_fence_without_schema_or_revision_changes(tmp_path):
    import sqlite3
    runtime = Runtime.create(root=tmp_path)
    source = SQLiteCandidateSource(runtime.store, projection_memory_only=True)
    reader = SQLiteProjectionReader(runtime.store, projection_memory_only=True)
    record = RecordEnvelope.create(kind='memory', title='restored memory', scope=ScopeRef())
    runtime.store.append(record)
    reader.page(ProjectionCursor(), limit=10)
    revision = source.authority_revision()
    schema = [tuple(row) for row in runtime.store.sqlite.conn.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
    version = runtime.store.sqlite.conn.execute('PRAGMA schema_version').fetchone()[0]
    runtime.close()
    attached = Runtime.create(root=tmp_path, bootstrap_schema=False)
    try:
        actual = SQLiteCandidateSource(attached.store, projection_memory_only=True)
        projection = SQLiteProjectionReader(attached.store, projection_memory_only=True)
        assert actual.authority_revision() == projection.snapshot_token() == revision
        head, fenced_revision = actual.authority_snapshot()
        assert fenced_revision == revision and head[1]
        assert projection.page(ProjectionCursor(), limit=10)[0]['record_id'] == record.record_id
        conn = attached.store.sqlite.conn
        assert [tuple(row) for row in conn.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')] == schema
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute('CREATE TABLE IF NOT EXISTS memory_vector_revision(singleton INTEGER,revision INTEGER)')
    finally:
        attached.close()


@pytest.mark.parametrize('missing', ['global_fence', 'memory_journal'])
def test_unused_optional_authority_does_not_block_old_database_attachment_or_get_recreated(tmp_path, missing):
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
    from eimemory.storage.code_evolution_store import CodeEvolutionStore

    runtime = Runtime.create(root=tmp_path)
    SQLiteCandidateSource(runtime.store, projection_memory_only=True)
    transaction_id = 'existing-ledger-with-unused-' + missing
    CodeEvolutionStore(runtime.store).create_transaction({
        'transaction_id': transaction_id, 'idempotency_key': transaction_id,
        'scope': {'tenant_id': 'fixture', 'agent_id': 'agent', 'workspace_id': 'workspace', 'user_id': 'owner'},
        'incident': {'incident_id': transaction_id, 'incident_class': 'fixture.existing_attachment'},
        'origin': 'manual_bootstrap', 'detector': 'fixture', 'known_before_detection': True,
        'prior_user_reported': True, 'manual_bootstrap': True,
        'repository': {'root': '/fixture/repo', 'remote': 'origin', 'ref': 'master', 'base_commit': 'a' * 40},
    })
    conn = runtime.store.sqlite.conn
    if missing == 'global_fence':
        for operation in ('insert', 'update', 'delete'):
            conn.execute('DROP TRIGGER trg_records_vector_sync_' + operation)
        conn.execute('DROP TABLE vector_sync_revision')
    else:
        conn.execute('DROP TABLE memory_vector_journal_contract')
    conn.commit()
    schema = [tuple(row) for row in conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    runtime.close()
    # Actual old-library attachment stays available for normalized ledger DML;
    # the absent, unused projection is never initialized by MemoryAPI.
    attached = Runtime.create(root=tmp_path, bootstrap_schema=False)
    try:
        manager = CodeEvolutionTransactionManager(attached, owner_id='existing-only-controller')
        manager.acquire_lease(transaction_id)
        manager.update_metadata(transaction_id, payload_updates={'optional_projection_unused': True})
        manager.renew_lease(transaction_id)
        manager.release_lease(transaction_id)
        assert manager.store.get_transaction(transaction_id)['payload']['optional_projection_unused'] is True
        optional = SQLiteCandidateSource(attached.store, projection_memory_only=missing == 'memory_journal')
        with pytest.raises(RuntimeError, match='existing_.*_schema_required'):
            optional.authority_revision()
        assert [tuple(row) for row in attached.store.sqlite.conn.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name')] == schema
        assert attached.store.sqlite.conn.execute('PRAGMA schema_version').fetchone()[0] == version
    finally:
        attached.close()
