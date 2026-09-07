from dataclasses import replace

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
