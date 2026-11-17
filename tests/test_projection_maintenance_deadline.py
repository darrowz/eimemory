"""Real maintenance entry points with delayed local embeddings and PG wire."""
from dataclasses import replace
from threading import Event, Timer
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from eimemory.ops import projection_acceptance_restore as restore
from eimemory.retrieval import postgres_cli
from eimemory.retrieval.postgres_vector import PostgresVectorCandidateSource
from test_incremental_projection_conflict_recovery import local, append, projected_ids
from test_projection_conflict_error_contract import native, apply


def restore_runtime(case, monkeypatch):
    case.provider.health = lambda: dict(configured=True, available=True, circuit='closed', last_error='')
    source = PostgresVectorCandidateSource.__new__(PostgresVectorCandidateSource)
    source.config = case.config

    def repository(config, **kwargs):
        case.main._maintenance_deadline_at = kwargs.get('deadline_at') or 0.0
        return case.main

    monkeypatch.setattr(postgres_cli, 'config_from_env', lambda: case.config)
    monkeypatch.setattr(postgres_cli, 'PostgresCandidateRepository', repository)
    return SimpleNamespace(store=case.store,
        memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))


def test_late_caught_up_callback_cannot_admit_expired_restore(tmp_path, monkeypatch):
    source = PostgresVectorCandidateSource.__new__(PostgresVectorCandidateSource)
    from eimemory.retrieval.postgres_vector import PostgresVectorConfig
    source.config = PostgresVectorConfig(enabled=True, dsn='synthetic', vector_dimension=3)
    (tmp_path / 'state').mkdir()
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path),
        memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))

    def late(runtime, **kwargs):
        sleep(0.06)
        return dict(ok=True, caught_up=True, complete=True)

    monkeypatch.setattr('eimemory.retrieval.vector_sync_worker.maintain_index', late)
    result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=0.02)
    assert result['ok'] is False
    assert 'deadline' in result['blocked_reason']


def test_uncooperative_embedding_returns_by_budget_and_never_commits_later(local, monkeypatch):
    runtime = restore_runtime(local, monkeypatch)
    append(local, 'late-embedding')
    entered, release = Event(), Event()
    original = local.provider.embed

    def delayed(texts, **kwargs):
        entered.set()
        release.wait(1)
        return original(texts, **kwargs)

    monkeypatch.setattr(local.provider, 'embed', delayed)
    timer = Timer(0.3, release.set)
    timer.start()
    started = monotonic()
    try:
        result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=0.06)
        elapsed = monotonic() - started
        assert entered.is_set()
        assert result['ok'] is False
        assert elapsed < 0.22
        assert local.wire.commits == []
    finally:
        release.set()
        timer.join()
    sleep(0.02)
    assert local.wire.commits == [] and projected_ids(local) == set()


def test_slow_delta_checkpoints_resume_without_losing_pending_records(local, monkeypatch):
    runtime = restore_runtime(local, monkeypatch)
    expected = {append(local, str(index)).record_id for index in range(8)}
    original = local.provider.embed

    def slow(texts, **kwargs):
        sleep(0.035)
        return original(texts, **kwargs)

    monkeypatch.setattr(local.provider, 'embed', slow)
    started = monotonic()
    first = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=0.12)
    assert monotonic() - started < 0.24
    assert first['ok'] is False
    committed = projected_ids(local)
    assert committed and committed < expected
    second = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=1)
    assert second['ok'] and projected_ids(local) == expected


def test_expired_native_delta_cannot_start_or_commit(monkeypatch):
    repository, wire, _, expected = native(monkeypatch)
    repository._maintenance_deadline_at = monotonic() - 1
    with pytest.raises(TimeoutError):
        apply(repository, expected)
    assert wire.commits == 0


def test_query_that_returns_after_budget_is_rolled_back_before_commit(monkeypatch):
    repository, wire, _, expected = native(monkeypatch)
    execute = wire.execute

    def delayed(sql, parameters=()):
        execute(sql, parameters)
        if sql.startswith('UPDATE'):
            sleep(0.06)

    monkeypatch.setattr(wire, 'execute', delayed)
    repository._maintenance_deadline_at = monotonic() + 0.02
    with pytest.raises(TimeoutError):
        apply(repository, expected)
    assert wire.commits == 0 and wire.rollbacks == 1


def test_late_commit_ack_is_an_unknown_failure(monkeypatch):
    repository, wire, _, expected = native(monkeypatch)
    commit = wire.commit

    def delayed_commit():
        commit()
        sleep(0.06)

    monkeypatch.setattr(wire, 'commit', delayed_commit)
    repository._maintenance_deadline_at = monotonic() + 0.02
    with pytest.raises(RuntimeError, match='projection_commit_outcome_unknown'):
        apply(repository, expected)
    assert wire.commits == 1


def test_busy_sqlite_lock_spends_the_same_restore_budget(local, monkeypatch):
    from threading import Thread
    runtime = restore_runtime(local, monkeypatch)
    append(local, 'locked-authority')
    acquired, release = Event(), Event()

    def hold():
        with local.store.locked():
            acquired.set()
            release.wait(1)

    holder = Thread(target=hold)
    holder.start()
    acquired.wait(1)
    started = monotonic()
    try:
        result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=0.04)
        assert not result['ok'] and 'deadline' in result['blocked_reason']
        assert monotonic() - started < 0.2
        assert local.provider.calls == 0 and local.wire.commits == []
    finally:
        release.set()
        holder.join(1)


def test_expired_delta_read_releases_its_own_transaction(local, monkeypatch):
    from eimemory.retrieval.incremental_sync import delta_snapshot
    from eimemory.retrieval.postgres_sync import SQLiteProjectionReader
    since = local.reader.snapshot_token()
    append(local, 'read-before-timeout')
    reader = SQLiteProjectionReader(local.store, projection_memory_only=True, deadline_at=monotonic() + 0.04)
    head = reader._memory_authority.head

    def late_head():
        sleep(0.06)
        return head()

    monkeypatch.setattr(reader._memory_authority, 'head', late_head)
    with pytest.raises(TimeoutError):
        delta_snapshot(reader, since=since, limit=2)
    assert not local.store.sqlite.in_transaction
    append(local, 'write-after-timeout')
    assert not local.store.sqlite.in_transaction


def test_late_connect_keeps_its_slot_until_the_connection_is_closed(monkeypatch):
    from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig
    from eimemory.retrieval.projection_deadline import ProjectionMaintenanceDeadlineExceeded
    entered, release, closed = Event(), Event(), Event()

    class Connection:
        def close(self):
            closed.set()

    def connect(**kwargs):
        entered.set()
        release.wait(1)
        return Connection()

    config = PostgresVectorConfig(enabled=True, connection_factory=connect, pool_size=1, queue_bound=1)
    repository = PostgresCandidateRepository(config, deadline_at=monotonic() + 0.04)
    try:
        with pytest.raises(ProjectionMaintenanceDeadlineExceeded):
            repository._connect()
        assert entered.is_set() and not closed.is_set()
        assert repository._gate.acquire(0.01) is False
    finally:
        release.set()
    assert closed.wait(1)
    assert repository._gate.acquire(0.1)
    repository._gate.release()


def test_bounded_native_statements_refresh_remaining_timeout(monkeypatch):
    from test_projection_conflict_error_contract import Wire
    from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig, IndexState
    wire = Wire()
    original = wire.execute
    timeouts = []

    def execute(sql, parameters=()):
        if "set_config('statement_timeout'" in sql:
            timeouts.append(int(parameters[0].removesuffix('ms')))
        else:
            sleep(0.015)
        return original(sql, parameters)

    monkeypatch.setattr(wire, 'execute', execute)
    config = PostgresVectorConfig(enabled=True, connection_factory=lambda **kwargs: wire,
        vector_dimension=3, projection_memory_only=True)
    repository = PostgresCandidateRepository(config, deadline_at=monotonic() + 0.25)
    expected = IndexState(ready=True, watermark='synthetic-generation', authority_revision='8',
        embedding_fingerprint='embedding-proof', projection_fingerprint='projection-proof',
        projection_digest_schema=wire.fetchone()['projection_digest_schema'])
    apply(repository, expected)
    assert wire.commits == 1
    assert all(0 < timeout <= 250 for timeout in timeouts)
    assert min(timeouts) < max(timeouts)


def test_outer_budget_resumes_bounded_maintenance_slices_before_read_proof(monkeypatch):
    from eimemory.governance.release import release_closure as closure
    from test_candidate_readiness_barrier import ready_measurement
    from eimemory.retrieval.projection_deadline import maintenance_owner
    now, calls, owners = [0.0], [], []
    source = object()
    runtime = SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))
    monkeypatch.setattr(closure.time if hasattr(closure, 'time') else __import__('time'),
                        'monotonic', lambda: now[0])

    def maintain(runtime, *, timeout_seconds):
        calls.append(('maintain', timeout_seconds))
        owners.append(maintenance_owner())
        now[0] += 120 if len(calls) == 1 else 20
        return dict(applicable=True, ok=len(calls) > 1,
            blocked_reason='projection_restore_deadline_exhausted' if len(calls) == 1 else '')

    measurement = ready_measurement()
    def barrier(runtime, *, timeout_seconds):
        calls.append(('barrier', timeout_seconds))
        return dict(ok=True, applicable=True, final_measurement=measurement)

    monkeypatch.setattr(restore, 'ensure_candidate_projection_restored', maintain)
    monkeypatch.setattr('eimemory.ops.candidate_readiness_barrier.wait_for_candidate_readiness', barrier)
    monkeypatch.setattr('eimemory.ops.controlled_stability._health',
                        lambda *args, **kwargs: dict(ok=True, postgres_readiness=measurement))
    result = closure._wait_for_controlled_projection(runtime, expected_commit='a' * 40)
    assert result['ok']
    assert calls == [('maintain', 120), ('maintain', 120), ('barrier', 160)]
    assert len(result['projection_acceptance_restore_rounds']) == 2
    assert result['elapsed_seconds'] == 140
    assert owners[0] == owners[1]
    assert maintenance_owner() != owners[0], 'The next operation cannot borrow the old lease owner'


def test_outer_budget_exhaustion_does_not_start_a_read_proof(monkeypatch):
    from eimemory.governance.release import release_closure as closure
    import time
    now, budgets = [0.0], []
    runtime = SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=object())))
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])

    def maintain(runtime, *, timeout_seconds):
        budgets.append(timeout_seconds)
        now[0] += timeout_seconds
        return dict(applicable=True, ok=False, blocked_reason='projection_restore_deadline_exhausted')

    monkeypatch.setattr(restore, 'ensure_candidate_projection_restored', maintain)
    monkeypatch.setattr('eimemory.ops.candidate_readiness_barrier.wait_for_candidate_readiness',
                        lambda *args, **kwargs: pytest.fail('No remaining accept budget'))
    result = closure._wait_for_controlled_projection(runtime, expected_commit='a' * 40)
    assert not result['ok'] and result['blocked_reason'] == 'projection_wait_exhausted'
    assert budgets == [120, 120, 60]


def test_outer_budget_preserves_real_maintenance_failure(monkeypatch):
    from eimemory.governance.release import release_closure as closure
    runtime = SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=object())))
    calls = []

    def maintain(runtime, *, timeout_seconds):
        calls.append(timeout_seconds)
        return dict(applicable=True, ok=False, blocked_reason='projection_commit_outcome_unknown')

    monkeypatch.setattr(restore, 'ensure_candidate_projection_restored', maintain)
    monkeypatch.setattr('eimemory.ops.candidate_readiness_barrier.wait_for_candidate_readiness',
                        lambda *args, **kwargs: pytest.fail('Unknown maintenance cannot admit'))
    result = closure._wait_for_controlled_projection(runtime, expected_commit='a' * 40)
    assert not result['ok'] and result['blocked_reason'] == 'projection_acceptance_restore_failed'
    assert len(calls) == 1


def test_pure_work_remains_bounded_after_multiple_late_results():
    from eimemory.retrieval.projection_deadline import pure_call, ProjectionMaintenanceDeadlineExceeded
    release = Event()
    entered, pending = [], []

    def work():
        entered.append(1)
        release.wait(1)
        return 'late'

    try:
        for _ in range(3):
            with pytest.raises(ProjectionMaintenanceDeadlineExceeded) as caught:
                pure_call(work, deadline_at=monotonic() + 0.025)
            if caught.value.pending is not None:
                pending.append(caught.value.pending)
        assert len(entered) == len(pending) == 2
    finally:
        release.set()
        for future in pending:
            future.result(timeout=1)
    assert pure_call(lambda: 'fresh', deadline_at=monotonic() + 0.2) == 'fresh'


def test_bounded_native_connection_publishes_all_current_projections(local):
    from test_incremental_projection_conflict_recovery import Connection, Cursor
    from eimemory.retrieval.incremental_sync import maintain_memory_projection
    from eimemory.retrieval.postgres_vector import PostgresCandidateRepository

    class QueryCursor(Cursor):
        def execute(self, sql, args=()):
            if sql.startswith(('SELECT ready, committed_watermark', 'SELECT c.storage_key')):
                return
            if sql.startswith('INSERT INTO'):
                return super().executemany(sql, [args])
            return super().execute(sql, args)

        def fetchall(self):
            return []

    class QueryConnection(Connection):
        def cursor(self):
            return QueryCursor(self)

    config = replace(local.config, connection_factory=lambda **kwargs: QueryConnection(local.wire, 'native-bounded'))
    repository = PostgresCandidateRepository(config)
    expected = {append(local, str(index)).record_id for index in range(3)}
    result = maintain_memory_projection(store=local.store, repository=repository, config=config,
        batch_size=32, max_pages=8, deadline_at=monotonic() + 1)
    assert result['ok'] and result['caught_up'] and result['processed'] == 3
    assert projected_ids(local) == expected
    assert repository._maintenance_deadline_at == 0, 'A bounded call cannot mutate a shared repository budget'


def test_uncooperative_pg_state_read_returns_by_budget_without_claiming_ready(local, monkeypatch):
    runtime = restore_runtime(local, monkeypatch)
    append(local, 'pending-state-read')
    original = local.main.read_index_state
    entered, release = Event(), Event()

    def delayed():
        entered.set()
        release.wait(1)
        return original()

    monkeypatch.setattr(local.main, 'read_index_state', delayed)
    timer = Timer(0.3, release.set)
    timer.start()
    started = monotonic()
    try:
        result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=0.05)
        assert entered.is_set() and not result['ok']
        assert monotonic() - started < 0.2
        assert local.provider.calls == 0 and local.wire.commits == []
    finally:
        release.set()
        timer.join()


def test_backend_error_while_budget_remains_is_preserved(local, monkeypatch):
    runtime = restore_runtime(local, monkeypatch)
    append(local, 'backend-failure')
    def failure(texts, **kwargs):
        raise RuntimeError('synthetic-backend-credential')
    monkeypatch.setattr(local.provider, 'embed', failure)
    result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=1)
    assert not result['ok'] and result['blocked_reason'] == 'memory_projection_maintenance_failed'
    assert result['maintenance']['reason'] == 'RuntimeError'
    assert 'synthetic-backend-credential' not in str(result)
    assert local.wire.commits == []


@pytest.mark.parametrize('commit_ack', [False, True])
def test_native_socket_deadline_interrupts_blocked_io_with_correct_commit_outcome(commit_ack):
    import socket
    import sys
    from test_projection_conflict_error_contract import Wire
    from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig, IndexState
    from eimemory.retrieval.projection_deadline import (
        ProjectionMaintenanceDeadlineExceeded, ProjectionCommitOutcomeUnknown,
        ProjectionDeadlineTransportUnsupported,
    )
    left, right = socket.socketpair()
    left.settimeout(1)

    class SocketWire(Wire):
        pgconn = SimpleNamespace(socket=left.fileno())

        def execute(self, sql, parameters=()):
            if not commit_ack and sql.startswith('SELECT ready'):
                if left.recv(1) == b'':
                    raise OSError('synthetic-socket-ended')
            return super().execute(sql, parameters)

        def commit(self):
            super().commit()
            if commit_ack and left.recv(1) == b'':
                raise OSError('synthetic-commit-ack-missing')

        def close(self):
            left.close()
            right.close()

    wire = SocketWire()
    config = PostgresVectorConfig(enabled=True, connection_factory=lambda **kwargs: wire,
        vector_dimension=3, projection_memory_only=True)
    repository = PostgresCandidateRepository(config, deadline_at=monotonic() + 0.06)
    expected = IndexState(ready=True, watermark='synthetic-generation', authority_revision='8',
        embedding_fingerprint='embedding-proof', projection_fingerprint='projection-proof',
        projection_digest_schema=wire.fetchone()['projection_digest_schema'])
    started = monotonic()
    try:
        error = (ProjectionDeadlineTransportUnsupported if sys.platform == 'win32' else
                 ProjectionCommitOutcomeUnknown if commit_ack else ProjectionMaintenanceDeadlineExceeded)
        with pytest.raises(error):
            apply(repository, expected)
        assert monotonic() - started < 0.22
        assert wire.commits == (0 if sys.platform == 'win32' else int(commit_ack))
    finally:
        wire.close()


def test_closed_watchdog_does_not_shutdown_the_original_socket():
    import socket
    import sys
    from eimemory.retrieval.projection_deadline import DeadlineSocket, ProjectionDeadlineTransportUnsupported
    left, right = socket.socketpair()
    try:
        if sys.platform == 'win32':
            with pytest.raises(ProjectionDeadlineTransportUnsupported):
                DeadlineSocket(SimpleNamespace(pgconn=SimpleNamespace(socket=left.fileno())), monotonic() + 0.02)
            right.sendall(b'x')
            left.settimeout(0.2)
            assert left.recv(1) == b'x'
            return
        guard = DeadlineSocket(SimpleNamespace(pgconn=SimpleNamespace(socket=left.fileno())), monotonic() + 0.02)
        guard.close()
        sleep(0.05)
        right.sendall(b'x')
        left.settimeout(0.2)
        assert left.recv(1) == b'x'
        assert not guard.expired.is_set()
    finally:
        left.close()
        right.close()
