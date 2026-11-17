"""Independent CAS interleavings with native delta SQL and real SQLite authority.

The PG wire is an in-memory transaction simulator. The production repository
evaluates its own FOR UPDATE/CAS condition; no fixture raises a CAS error.
Only the local embedding boundary is scheduled between two maintainers.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json
import socket

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.incremental_sync import maintain_memory_projection
from eimemory.retrieval.postgres_sync import SQLiteProjectionReader
from eimemory.retrieval.postgres_vector import (
    IndexState, PostgresCandidateRepository, PostgresVectorConfig,
    PROJECTION_DIGEST_SCHEMA, projection_fingerprint,
)
from eimemory.storage.runtime_store import RuntimeStore


class Embeddings:
    max_batch = 1

    def __init__(self):
        self.calls = 0
        self.interleave = None

    def fingerprint(self):
        return 'e' * 64

    def embed(self, texts, **kwargs):
        self.calls += 1
        callback, self.interleave = self.interleave, None
        if callback is not None:
            callback()
        return [(0.1, 0.2, 0.3) for _ in texts]


class Wire:
    def __init__(self, state):
        self.state = state
        self.rows = {}
        self.in_progress = False
        self.database_error = ''
        self.commits = []
        self.rollbacks = 0


class Connection:
    def __init__(self, wire, writer):
        self.wire, self.writer = wire, writer
        self.state, self.rows = wire.state, deepcopy(wire.rows)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.wire.state, self.wire.rows = self.state, self.rows
        self.wire.commits.append((self.writer, self.state.authority_revision))

    def rollback(self):
        self.wire.rollbacks += 1

    def close(self):
        pass


class Cursor:
    description = None

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=()):
        connection = self.connection
        if "set_config('statement_timeout'" in sql:
            return
        if sql.startswith('SELECT ready,in_progress,committed_watermark'):
            if connection.wire.database_error:
                raise RuntimeError(connection.wire.database_error)
            assert 'FOR UPDATE' in sql
            return
        if sql.startswith('DELETE FROM'):
            watermark, keys = args
            assert watermark == connection.state.watermark
            for key in keys:
                connection.rows.pop(key, None)
            return
        if sql.startswith('UPDATE') and 'SET authority_revision=' in sql:
            revision, updated, key = args
            connection.state = replace(connection.state, authority_revision=revision,
                authoritative_updated_at=updated, authoritative_storage_key=key)
            return
        raise AssertionError('Unexpected native SQL: ' + sql)

    def fetchone(self):
        state = self.connection.state
        return dict(ready=state.ready, in_progress=self.connection.wire.in_progress,
            committed_watermark=state.watermark, authority_revision=state.authority_revision,
            embedding_fingerprint=state.embedding_fingerprint,
            projection_fingerprint=state.projection_fingerprint,
            projection_digest_schema=state.projection_digest_schema)

    def executemany(self, sql, values):
        assert sql.startswith('INSERT INTO')
        for row in values:
            self.connection.rows[row[0]] = dict(record_id=row[1], vector=tuple(json.loads(row[9])),
                projection_digest=row[14], title=row[10], status=row[8])


class NativeRepository(PostgresCandidateRepository):
    """Retain native apply_memory_delta, changing transport and cache reads only."""
    def __init__(self, config, wire, writer):
        super().__init__(config)
        self.wire, self.writer = wire, writer
        self.reads = 0

    def _connect(self, **kwargs):
        return Connection(self.wire, self.writer)

    def read_index_state(self):
        self.reads += 1
        return self.wire.state

    def reusable_embeddings(self, *, projection_digests, embedding_fingerprint, projection_fingerprint):
        return {key: self.wire.rows[key]['vector'] for key, value in projection_digests.items()
                if key in self.wire.rows and self.wire.rows[key]['projection_digest'] == value}


@pytest.fixture
def local(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No network or production PG in independent CAS tests')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    store = RuntimeStore(tmp_path / 'authority')
    reader = SQLiteProjectionReader(store, projection_memory_only=True)
    provider, peer_provider = Embeddings(), Embeddings()
    config = PostgresVectorConfig(enabled=True, dsn='synthetic-no-network', vector_dimension=3,
        projection_memory_only=True, embedding_provider=provider)
    wire = Wire(IndexState(ready=True, watermark='independent-generation',
        authority_revision=reader.snapshot_token(), embedding_fingerprint=provider.fingerprint(),
        projection_fingerprint=projection_fingerprint(config), projection_digest_schema=PROJECTION_DIGEST_SCHEMA))
    case = SimpleNamespace(store=store, reader=reader, provider=provider, config=config, wire=wire,
        peer_config=replace(config, embedding_provider=peer_provider), peer_provider=peer_provider)
    case.main = NativeRepository(config, wire, 'main')
    case.peer = NativeRepository(case.peer_config, wire, 'peer')
    try:
        yield case
    finally:
        store.close()


def append(case, label):
    return case.store.append(RecordEnvelope.create(kind='memory', title='Synthetic inspection ' + label,
        summary='Inspection card ' + label, content={'text': 'Inspection card ' + label},
        scope=ScopeRef(tenant_id='independent-cas', agent_id='fixture', workspace_id='lab', user_id='owner'),
        source='independent.fixture', source_id='fixture'))


def run(case, *, peer=False, pages=4, batch=2):
    return maintain_memory_projection(store=case.store, repository=case.peer if peer else case.main,
        config=case.peer_config if peer else case.config, batch_size=batch, max_pages=pages)


def projected_ids(case):
    return {row['record_id'] for row in case.wire.rows.values()}


def test_normal_native_delta_still_commits_each_current_record(local):
    expected = {append(local, str(n)).record_id for n in range(3)}
    result = run(local)
    assert result['ok'] and result['caught_up'] and result['processed'] == 3
    assert projected_ids(local) == expected
    assert local.wire.rollbacks == 0


def test_peer_completes_same_generation_while_main_embeds_without_duplicate_commit(local):
    expected = {append(local, str(n)).record_id for n in range(2)}
    local.provider.interleave = lambda: run(local, peer=True)
    result = run(local)
    assert result['ok'] and result['complete'] and result['caught_up']
    assert result['processed'] == result['pages'] == 0, 'The failed CAS did not commit this batch'
    assert projected_ids(local) == expected
    assert local.wire.rollbacks == 1
    assert all(writer == 'peer' for writer, _ in local.wire.commits)
    assert local.wire.state.authority_revision == local.reader.snapshot_token()


def test_peer_partial_progress_is_followed_by_fresh_delta_without_losing_later_memory(local):
    expected = {append(local, str(n)).record_id for n in range(3)}
    def race():
        peer = run(local, peer=True, pages=1, batch=2)
        assert peer['ok'] and not peer['caught_up']
        expected.add(append(local, 'arrived-during-embedding').record_id)
    local.provider.interleave = race
    result = run(local, pages=4, batch=2)
    assert result['ok'] and result['caught_up']
    assert result['processed'] == 2
    assert projected_ids(local) == expected
    assert local.wire.rollbacks == 1


@pytest.mark.parametrize('change', ['watermark', 'embedding_fingerprint', 'projection_fingerprint',
    'schema', 'future_revision', 'backward_revision', 'no_progress', 'not_ready'])
def test_conflict_never_accepts_changed_identity_or_unjustified_revision(local, change):
    append(local, 'already-committed')
    assert run(local, peer=True)['caught_up']
    append(local, 'pending')
    original = local.wire.state
    commits = list(local.wire.commits)
    def mutate():
        mutations = {
            'watermark': dict(watermark='different-generation'),
            'embedding_fingerprint': dict(embedding_fingerprint='b' * 64),
            'projection_fingerprint': dict(projection_fingerprint='b' * 64),
            'schema': dict(projection_digest_schema='different-schema'),
            'future_revision': dict(authority_revision=str(int(local.reader.snapshot_token()) + 1)),
            'backward_revision': dict(authority_revision=str(int(original.authority_revision) - 1)),
            'not_ready': dict(ready=False),
        }
        if change == 'no_progress':
            local.wire.in_progress = True  # Native CAS fails, but re-read revision has not progressed.
        else:
            local.wire.state = replace(original, **mutations[change])
    local.provider.interleave = mutate
    with pytest.raises(RuntimeError):
        run(local, pages=3)
    assert local.wire.commits == commits
    assert local.provider.calls == 1, 'An unqualified conflict must not launch another embedding batch'
    assert local.wire.rollbacks == 1


@pytest.mark.parametrize('message', ['synthetic_database_failure', 'postgres_delta_conflict'])
def test_database_failure_or_same_text_exception_is_not_retried_as_native_cas(local, message):
    append(local, 'pending')
    local.wire.database_error = message
    with pytest.raises(RuntimeError):
        run(local, pages=3)
    assert local.wire.commits == []
    assert local.wire.rollbacks == 1 and local.provider.calls == 1


def test_repeated_real_competition_consumes_original_page_budget_without_false_completion(local):
    expected = {append(local, 'initial').record_id}
    races = []
    def race():
        assert run(local, peer=True)['caught_up']
        races.append(1)
        expected.add(append(local, 'new-' + str(len(races))).record_id)
        local.provider.interleave = race
    local.provider.interleave = race
    with pytest.raises(RuntimeError):
        run(local, pages=2, batch=2)
    assert len(races) == 2 and local.provider.calls == 2
    assert local.wire.rollbacks == 2
    assert all(writer == 'peer' for writer, _ in local.wire.commits)
    assert int(local.wire.state.authority_revision) < int(local.reader.snapshot_token())
    assert run(local, peer=True)['caught_up']
    assert projected_ids(local) == expected
