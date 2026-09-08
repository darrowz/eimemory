"""Optional real PostgreSQL lifecycle check using synthetic, isolated data only."""
from dataclasses import replace
import os
from uuid import uuid4

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.incremental_sync import maintain_memory_projection
from eimemory.retrieval.postgres_sync import SQLiteProjectionReader, ProjectionCursor
from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig, _derived_identifier
from eimemory.storage.runtime_store import RuntimeStore


class SyntheticProvider:
    max_batch = 4

    def fingerprint(self):
        return 'e' * 64

    def embed(self, texts, **kwargs):
        return [(1., 0., 0.) for _ in texts]


@pytest.mark.skipif(not os.environ.get('EIMEMORY_TEST_POSTGRES_DSN'), reason='explicit test PostgreSQL DSN required')
def test_real_fragment_lifecycle_and_authority_partition(tmp_path):
    config = PostgresVectorConfig(enabled=True, dsn=os.environ['EIMEMORY_TEST_POSTGRES_DSN'],
        vector_dimension=3, embedding_provider=SyntheticProvider(), projection_memory_only=True,
        evidence_fragments=True, table='fragment_test_' + uuid4().hex[:12])
    repo = PostgresCandidateRepository(config)
    store = RuntimeStore(tmp_path / 'authority')
    scope = ScopeRef(user_id='test-owner')
    created = False
    try:
        assert repo.migrate()['ok']
        created = True
        item = RecordEnvelope.create(kind='memory', title='阅读偏好',
            summary='阅读微信文章正文；不要只看标题。', scope=scope)
        store.append(item)
        store.append(RecordEnvelope.create(kind='memory', title='private', summary='微信文章',
            scope=ScopeRef(user_id='other-user')))

        def sync():
            result = maintain_memory_projection(store=store, repository=repo, config=config,
                                                 batch_size=4, max_pages=5)
            assert result['caught_up'], result

        sync()
        state = repo.read_index_state()
        request = CandidateRequest.create(query='微信文章', scope=scope, kinds=['memory'], limit=10)

        def search():
            return repo.search(request, (1., 0., 0.), top_k=10, watermark=state.watermark)

        rows = search()
        assert [r['record_id'] for r in rows] == [item.record_id]
        old_ids = {r['fragment_id'] for r in rows}
        store.append(replace(item, summary='阅读公众号全文；不是只看标题。'))
        sync()
        assert not old_ids & {r['fragment_id'] for r in search()}
        reader = SQLiteProjectionReader(store, projection_memory_only=True)
        key = next(r['storage_key'] for r in reader.page(ProjectionCursor(), limit=10)
                   if r['record_id'] == item.record_id)
        store.sqlite.conn.execute('DELETE FROM records WHERE storage_key=?', (key,))
        store.sqlite.conn.commit()
        sync()
        assert not search()
        with pytest.raises(RuntimeError):
            repo.apply_memory_delta(expected_state=state, projections=[], changed_keys=[],
                                    authority_revision=state.authority_revision, authoritative_head=('', ''))
        connection = repo._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT count(*) AS n FROM {repo.qualified_fragment_table} WHERE storage_key=%s', (key,))
                assert cursor.fetchone()['n'] == 0
        finally:
            connection.close()
    finally:
        store.close()
        if created:
            # Only the UUID-named tables created by this test, never a live index.
            connection = repo._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP TABLE {repo.qualified_fragment_table}')
                    cursor.execute(f'DROP TABLE {repo.qualified_table}')
                    cursor.execute(f'DROP TABLE {repo.qualified_state_table}')
                    cursor.execute(f'DROP TABLE "{config.schema}"."{_derived_identifier(config, "migrations")}"')
                connection.commit()
            finally:
                connection.close()
