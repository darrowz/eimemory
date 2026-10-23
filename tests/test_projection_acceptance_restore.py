"""Pre-accept projection restore must clear before the readiness accept budget."""
from types import SimpleNamespace

import pytest

pytest_plugins = ['test_controlled_production_acceptance']

from eimemory.ops import projection_acceptance_restore as restore
from eimemory.retrieval.postgres_vector import PostgresVectorConfig
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from test_candidate_readiness_barrier import Clock, native_source, runtime_for


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(restore, 'monotonic', value.monotonic)
    monkeypatch.setattr(restore, 'sleep', value.sleep)
    return value


def runtime_with_root(source, tmp_path):
    runtime = runtime_for(source)
    runtime.store = SimpleNamespace(root=tmp_path)
    (tmp_path / 'state').mkdir(parents=True, exist_ok=True)
    return runtime


def test_sqlite_and_unconfigured_sources_skip_without_maintenance(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        'eimemory.retrieval.vector_sync_worker.maintain_index',
        lambda runtime: calls.append(runtime) or {'ok': True},
    )
    sqlite = SQLiteCandidateSource.__new__(SQLiteCandidateSource)
    result = restore.ensure_candidate_projection_restored(runtime_with_root(sqlite, tmp_path))
    assert result['ok'] and result['applicable'] is False and result['skipped'] == 'sqlite'
    assert calls == []

    source, _, _ = native_source(PostgresVectorConfig(enabled=True, dsn='', vector_dimension=3))
    result = restore.ensure_candidate_projection_restored(runtime_with_root(source, tmp_path))
    assert result['ok'] and result['skipped'] == 'not_configured' and calls == []


def test_waits_for_storage_guard_then_catches_up_before_accept_budget(tmp_path, clock, monkeypatch):
    source, _, _ = native_source()
    runtime = runtime_with_root(source, tmp_path)
    marker = tmp_path / 'state' / 'storage-release-transaction.json'
    marker.write_text('{}', encoding='utf-8')
    rounds = []

    def maintain(runtime_arg):
        rounds.append(restore.monotonic())
        assert not marker.exists()
        return {'ok': True, 'skipped': 'already_current', 'watermark': 'w'}

    monkeypatch.setattr('eimemory.retrieval.vector_sync_worker.maintain_index', maintain)
    clock.after_sleep = lambda: marker.unlink(missing_ok=True)
    result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=5)
    assert result['ok'] and result['applicable'] is True
    assert result['storage_release_state'] == 'clean'
    assert result['wait_seconds'] > 0
    assert result['maintenance_rounds'] == 1
    assert result['maintenance']['skipped'] == 'already_current'
    assert rounds == [clock.now]


def test_incomplete_sync_retries_until_caught_up(tmp_path, clock, monkeypatch):
    source, _, _ = native_source()
    runtime = runtime_with_root(source, tmp_path)
    responses = [
        {'ok': True, 'complete': False, 'processed': 2},
        {'ok': True, 'complete': True, 'caught_up': True, 'processed': 1, 'authority_revision': '9'},
    ]

    def maintain(runtime_arg):
        return responses.pop(0)

    monkeypatch.setattr('eimemory.retrieval.vector_sync_worker.maintain_index', maintain)
    result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=5)
    assert result['ok'] and result['maintenance_rounds'] == 2
    assert result['maintenance']['caught_up'] is True
    assert responses == []


def test_storage_guard_timeout_fails_before_maintenance(tmp_path, clock, monkeypatch):
    source, _, _ = native_source()
    runtime = runtime_with_root(source, tmp_path)
    (tmp_path / 'state' / 'storage-release-transaction.json').write_text('{}', encoding='utf-8')
    calls = []
    monkeypatch.setattr(
        'eimemory.retrieval.vector_sync_worker.maintain_index',
        lambda runtime: calls.append(1) or {'ok': True, 'skipped': 'already_current'},
    )
    result = restore.ensure_candidate_projection_restored(runtime, timeout_seconds=1)
    assert not result['ok']
    assert result['blocked_reason'] == 'storage_release_guard_not_clear'
    assert calls == []


def test_cpa_invokes_restore_before_readiness_barrier(setup, monkeypatch):
    from eimemory.evaluation import controlled_production_acceptance as cpa
    from eimemory.ops import candidate_readiness_barrier as barrier
    from test_candidate_readiness_barrier import ready_measurement
    from test_controlled_production_acceptance import OWNER, configured_pg, run

    source, _ = configured_pg(setup)
    order = []

    def fake_restore(runtime, *, timeout_seconds=120.0):
        order.append(('restore', timeout_seconds))
        return dict(
            schema=restore.SCHEMA, ok=True, applicable=False,
            skipped='candidate_source_config_unavailable', blocked_reason='',
            storage_release_state='', maintenance={}, maintenance_rounds=0,
            started_at='t0', completed_at='t1', elapsed_seconds=0.0,
            timeout_seconds=timeout_seconds, wait_seconds=0.0,
        )

    def fake_wait(runtime, timeout_seconds=300):
        order.append(('barrier', timeout_seconds))
        measurement = ready_measurement()
        completed = measurement['observed_at']
        return dict(
            schema=barrier.SCHEMA, ok=True, applicable=True, ready=True, status='ready',
            blocked_reason='', source_type='postgres_vector',
            started_at=completed, completed_at=completed,
            elapsed_seconds=1.0, timeout_seconds=timeout_seconds,
            probe_timeout_seconds=0.2, attempt_count=1, error_code_counts={},
            final_measurement=measurement,
        )

    monkeypatch.setattr(cpa, 'ensure_candidate_projection_restored', fake_restore)
    monkeypatch.setattr(cpa, 'wait_for_candidate_readiness', fake_wait)
    monkeypatch.setattr(source, 'readiness', lambda **kwargs: ready_measurement())
    result = run(setup)
    assert [step[0] for step in order] == ['restore', 'barrier']
    assert result['accepted']
    assert result['projection_acceptance_restore']['schema'] == restore.SCHEMA
    packet = setup.runtime.store.get_by_exact_ref(
        result['record_id'], scope=OWNER, source_id='default')
    assert packet.content['projection_acceptance_restore']['skipped'] == (
        'candidate_source_config_unavailable')


def test_cpa_skips_accept_barrier_when_applicable_restore_fails(setup, monkeypatch):
    from eimemory.evaluation import controlled_production_acceptance as cpa
    from test_controlled_production_acceptance import configured_pg, run

    configured_pg(setup)
    order = []

    def fake_restore(runtime, *, timeout_seconds=120.0):
        order.append('restore')
        return dict(
            schema=restore.SCHEMA, ok=False, applicable=True, skipped='',
            blocked_reason='storage_release_guard_not_clear',
            storage_release_state='in_progress', maintenance={}, maintenance_rounds=0,
            started_at='t0', completed_at='t1', elapsed_seconds=1.0,
            timeout_seconds=timeout_seconds, wait_seconds=1.0,
        )

    def fake_wait(*args, **kwargs):
        order.append('barrier')
        raise AssertionError('accept barrier must not start after restore failure')

    monkeypatch.setattr(cpa, 'ensure_candidate_projection_restored', fake_restore)
    monkeypatch.setattr(cpa, 'wait_for_candidate_readiness', fake_wait)
    result = run(setup)
    assert order == ['restore']
    assert not result['accepted']
    assert result['blocked_reason'] == 'controlled_acceptance_candidate_readiness_unverified'
    assert result['candidate_readiness_barrier']['blocked_reason'] == (
        'projection_acceptance_restore_failed')
    assert result['projection_acceptance_restore']['blocked_reason'] == (
        'storage_release_guard_not_clear')
