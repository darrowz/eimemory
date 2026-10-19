"""Deterministic waiting retains the native SELECT, fences and hard budgets."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from eimemory.ops import candidate_readiness_barrier as barrier
from eimemory.retrieval.contracts import CandidateBatch
from eimemory.retrieval.postgres_vector import (IndexState, PostgresVectorCandidateSource,
    PostgresVectorConfig, PROJECTION_DIGEST_SCHEMA, projection_fingerprint)
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource


class Clock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []
        self.after_sleep = lambda: None
    def monotonic(self): return self.now
    def sleep(self, seconds):
        assert 0 < seconds <= .5
        self.sleeps.append(seconds)
        self.now += seconds
        self.after_sleep()


def ready_measurement(**changes):
    value = dict(schema='postgres_candidate_readiness.v1',
        measurement_origin='native_readonly_vector_query', ready=True, enabled=True, configured=True,
        embedding_available=True, index_verified=True, backend_query_verified=True,
        authority_unchanged=True, fingerprints_match=True, revision_matches=True,
        lag_seconds=0.0, watermark='native-fixture-watermark', authority_revision='7',
        circuit='closed', last_error='', error='', observed_at=datetime.now(timezone.utc).isoformat())
    value.update(changes)
    return value


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(barrier, 'monotonic', value.monotonic)
    monkeypatch.setattr(barrier, 'sleep', value.sleep)
    return value


def native_source(config=None):
    config = config or PostgresVectorConfig(enabled=True, dsn='postgresql://fixture-never-connected', vector_dimension=3)
    class Authority:
        head = ('2026-10-18T00:00:00.000000Z', 'native-fixture-head')
        revision = '7'
        def authority_snapshot(self, *, deadline_at=0):
            assert deadline_at > 0
            return self.head, self.revision
        def search(self, request): return CandidateBatch(hits=())
    class Provider:
        calls = 0
        def fingerprint(self): return 'f' * 64
        def embed(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('readiness must not embed')
        def health(self): return dict(configured=True, available=True, circuit='closed', dimension=3, last_error='')
    authority = Authority()
    class Repository:
        state = IndexState(ready=True, watermark='native-fixture-watermark', lag_seconds=0,
            authoritative_updated_at=authority.head[0], authoritative_storage_key=authority.head[1],
            authority_revision='6', embedding_fingerprint='f' * 64,
            projection_digest_schema=PROJECTION_DIGEST_SCHEMA, projection_fingerprint=projection_fingerprint(config))
        searches = 0
        def read_index_state(self, *, deadline_at=0):
            assert deadline_at > 0
            return self.state
        def search(self, request, vector, *, top_k, watermark):
            self.searches += 1
            assert request.scope.user_id == 'eimemory-readiness-private-v1'
            assert request.source_ids == ('eimemory-readiness-private-v1',)
            assert request.limit == top_k == 1
            assert vector == (1.0, 0.0, 0.0)
            return []
    repository, provider = Repository(), Provider()
    source = PostgresVectorCandidateSource(sqlite_source=authority, config=config,
        repository=repository, embedding_provider=provider)
    return source, repository, provider


def runtime_for(source):
    return SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))


def probe_source(monkeypatch, fn):
    source = PostgresVectorCandidateSource.__new__(PostgresVectorCandidateSource)
    monkeypatch.setattr(source, 'readiness', fn)
    return source


def test_native_lag_waits_then_runs_real_private_vector_select_without_embedding_or_query_proof(clock, monkeypatch):
    source, repository, provider = native_source()
    calls = []
    native_probe = source.readiness
    def probe(**kwargs):
        calls.append(kwargs)
        return native_probe(**kwargs)
    monkeypatch.setattr(source, 'readiness', probe)
    old_query = (source._last_query_valid, source._last_query_index_identity,
        source._backend_query_identity, source._last_query_status, source._last_error)
    clock.after_sleep = lambda: setattr(repository, 'state', replace(repository.state, authority_revision='7'))
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert result['ok'] and result['applicable'] and result['ready']
    assert result['attempt_count'] == 2 and result['error_code_counts'] == {'authority_revision_changed': 1}
    assert calls == [dict(force=True, timeout_seconds=.2)] * 2
    assert repository.searches == 1 and provider.calls == 0
    assert old_query == (source._last_query_valid, source._last_query_index_identity,
        source._backend_query_identity, source._last_query_status, source._last_error)
    assert barrier.candidate_readiness_barrier_ok(result, postgres=True)


def test_persistent_native_lag_times_out_without_private_select_or_embedding(clock):
    source, repository, provider = native_source()
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['status'] == 'timeout' and not result['ready']
    assert result['elapsed_seconds'] == 1 and result['attempt_count'] == 2
    assert result['error_code_counts'] == {'authority_revision_changed': 2}
    assert repository.searches == provider.calls == 0
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=True)


def test_remaining_deadline_is_passed_to_probe_without_widening_200ms(clock, monkeypatch):
    calls = []
    def probe(**kwargs):
        calls.append(kwargs)
        return ready_measurement() if len(calls) == 2 else ready_measurement(ready=False, error='index_lag_exceeded')
    result = barrier.wait_for_candidate_readiness(runtime_for(probe_source(monkeypatch, probe)), timeout_seconds=.55)
    assert result['ok'] and len(calls) == 2
    assert calls[0] == dict(force=True, timeout_seconds=.2)
    assert calls[1]['force'] is True and calls[1]['timeout_seconds'] == pytest.approx(.05)
    assert clock.sleeps == [.5]


def test_late_positive_measurement_cannot_certify_an_expired_wait(clock, monkeypatch):
    def probe(**kwargs):
        clock.now += .21
        return ready_measurement()
    result = barrier.wait_for_candidate_readiness(runtime_for(probe_source(monkeypatch, probe)), timeout_seconds=.2)
    assert not result['ok'] and not result['ready'] and result['status'] == 'timeout'
    assert result['final_measurement']['ready'] is True
    assert result['error_code_counts'] == {'readiness_probe_deadline_exceeded': 1}
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=True)


def test_late_single_probe_is_not_accepted_even_with_global_budget_remaining(clock, monkeypatch):
    def probe(**kwargs):
        clock.now += .21
        return ready_measurement()
    result = barrier.wait_for_candidate_readiness(runtime_for(probe_source(monkeypatch, probe)), timeout_seconds=1)
    assert not result['ok'] and result['attempt_count'] == 2
    assert result['error_code_counts'] == {'readiness_probe_deadline_exceeded': 2}


@pytest.mark.parametrize('source', [None, SimpleNamespace(configured=False),
    SimpleNamespace(configured=True), SimpleNamespace(readiness=lambda **kwargs:ready_measurement())])
def test_unknown_source_cannot_claim_not_applicable_or_inject_a_ready_dictionary(clock, source):
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['applicable'] is True and not result['ready']
    assert result['attempt_count'] == 0 and result['blocked_reason'] == 'candidate_source_unsupported'


def test_only_exact_native_sqlite_source_is_explicitly_inapplicable(clock):
    source = SQLiteCandidateSource.__new__(SQLiteCandidateSource)
    result = barrier.wait_for_candidate_readiness(runtime_for(source))
    assert result['ok'] and result['applicable'] is False and not result['ready']
    assert result['status'] == 'not_applicable' and result['attempt_count'] == 0
    assert barrier.candidate_readiness_barrier_ok(result, postgres=False)
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=True)
    class Unknown(SQLiteCandidateSource): pass
    assert not barrier.wait_for_candidate_readiness(runtime_for(Unknown.__new__(Unknown)))['ok']


@pytest.mark.parametrize('enabled,dsn,reason', [
    (False, 'postgresql://fixture-never-connected', 'disabled'),
    (False, '', 'disabled'), (True, '', 'not_configured')])
def test_explicit_native_pg_bypass_is_inapplicable_without_backend_or_embedding(clock, enabled, dsn, reason):
    source, repository, provider = native_source(PostgresVectorConfig(enabled=enabled, dsn=dsn, vector_dimension=3))
    before = (source._last_query_valid, source._last_query_index_identity, source._backend_query_identity,
              source._last_query_status, source._last_error)
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert result['ok'] and result['status'] == 'not_applicable'
    assert result['applicable'] is False and result['ready'] is False
    assert result['bypass_reason'] == reason and result['attempt_count'] == 1
    assert result['source_configuration'] == dict(enabled=enabled, configured=bool(dsn))
    assert result['final_measurement']['error'] == result['final_measurement']['last_error'] == reason
    assert result['error_code_counts'] == {} and clock.sleeps == []
    assert repository.searches == provider.calls == 0
    assert before == (source._last_query_valid, source._last_query_index_identity, source._backend_query_identity,
                      source._last_query_status, source._last_error)
    assert barrier.candidate_readiness_barrier_ok(result, postgres=True, source=source)
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=False, source=source)
    configured, _, _ = native_source()
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=True, source=configured)


def test_pg_becoming_configured_during_real_probe_cannot_reuse_bypass(clock, monkeypatch):
    source, repository, provider = native_source(PostgresVectorConfig(enabled=True, dsn='', vector_dimension=3))
    native_probe = source.readiness
    def probe(**kwargs):
        measurement = native_probe(**kwargs)
        source.config = replace(source.config, dsn='postgresql://fixture-never-connected')
        return measurement
    monkeypatch.setattr(source, 'readiness', probe)
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['blocked_reason'] == 'candidate_source_changed'
    assert result['applicable'] is True and repository.searches == provider.calls == 0


@pytest.mark.parametrize('reason', ['disabled', 'not_configured'])
def test_configured_pg_cannot_inject_bypass_measurement(clock, monkeypatch, reason):
    disabled, _, _ = native_source(PostgresVectorConfig(enabled=reason != 'disabled', dsn='', vector_dimension=3))
    forged = disabled.readiness(force=True, timeout_seconds=.2)
    source, repository, provider = native_source()
    monkeypatch.setattr(source, 'readiness', lambda **kwargs: deepcopy(forged))
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['status'] == 'timeout' and result['applicable'] is True
    assert result['error_code_counts'] == {reason: 2}
    assert repository.searches == provider.calls == 0


@pytest.mark.parametrize('change', [dict(error='postgres_timeout', last_error='postgres_timeout'),
    dict(error='unknown', last_error='unknown'), dict(error='index_not_ready', last_error='index_not_ready'),
    dict(ready=True), dict(enabled=False), dict(index_verified=True), dict(backend_query_verified=True),
    dict(watermark='stale-proof'), dict(observed_at='2020-01-01T00:00:00Z')])
def test_unconfigured_pg_requires_consistent_native_bypass_measurement(clock, monkeypatch, change):
    source, repository, provider = native_source(PostgresVectorConfig(enabled=True, dsn='', vector_dimension=3))
    measurement = source.readiness(force=True, timeout_seconds=.2)
    measurement.update(change)
    monkeypatch.setattr(source, 'readiness', lambda **kwargs: deepcopy(measurement))
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['blocked_reason'] == 'readiness_contract_invalid'
    assert result['applicable'] is True and result['attempt_count'] == 1
    assert repository.searches == provider.calls == 0


def test_pg_subclass_cannot_claim_configuration_exemption(clock):
    source, _, _ = native_source(PostgresVectorConfig(enabled=False, vector_dimension=3))
    class Unknown(PostgresVectorCandidateSource): pass
    source.__class__ = Unknown
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['applicable'] is True
    assert result['status'] == 'timeout' and result['error_code_counts'] == {'disabled': 2}


@pytest.mark.parametrize('field,value', [('bypass_reason', 'index_not_ready'),
    ('source_configuration', dict(enabled=True, configured=True)), ('attempt_count', 0),
    ('error_code_counts', {'disabled': 1}), ('ready', True)])
def test_saved_pg_configuration_exemption_cannot_be_forged(clock, field, value):
    source, _, _ = native_source(PostgresVectorConfig(enabled=False, vector_dimension=3))
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert barrier.candidate_readiness_barrier_ok(result, postgres=True, source=source)
    result[field] = value
    assert not barrier.candidate_readiness_barrier_ok(result, postgres=True, source=source)


def test_failed_optional_pg_construction_cannot_be_hidden_as_sqlite_not_applicable(clock):
    runtime = runtime_for(SQLiteCandidateSource.__new__(SQLiteCandidateSource))
    runtime.candidate_source_error = 'invalid private backend configuration'
    result = barrier.wait_for_candidate_readiness(runtime)
    assert not result['ok'] and result['applicable'] is True and not result['ready']
    assert result['blocked_reason'] == 'candidate_source_unavailable'
    assert 'private' not in str(result)


def test_native_pg_missing_api_or_raising_api_fails_closed_without_private_error(clock, monkeypatch):
    source = probe_source(monkeypatch, None)
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['blocked_reason'] == 'readiness_probe_unavailable'
    def broken(**kwargs): raise RuntimeError('private connection secret')
    monkeypatch.setattr(source, 'readiness', broken)
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=1)
    assert not result['ok'] and result['error_code_counts'] == {'readiness_probe_unavailable': 2}
    assert 'private' not in str(result)


def test_source_replacement_during_probe_revokes_even_valid_ready_result(clock, monkeypatch):
    runtime = runtime_for(None)
    def probe(**kwargs):
        runtime.memory.recall_engine.candidate_source = SQLiteCandidateSource.__new__(SQLiteCandidateSource)
        return ready_measurement()
    runtime.memory.recall_engine.candidate_source = probe_source(monkeypatch, probe)
    result = barrier.wait_for_candidate_readiness(runtime, timeout_seconds=1)
    assert not result['ok'] and result['blocked_reason'] == 'candidate_source_changed'


@pytest.mark.parametrize('change', [dict(authority_unchanged=False),dict(backend_query_verified=False),
    dict(revision_matches=False),dict(schema='foreign'),dict(measurement_origin='metadata'),
    dict(lag_seconds=float('nan')),dict(lag_seconds=True),dict(lag_seconds=-1),dict(watermark=''),
    dict(authority_revision=''),dict(circuit='open'),dict(observed_at='2020-01-01T00:00:00Z')])
def test_partial_metadata_stale_or_malformed_measurements_never_pass(clock, monkeypatch, change):
    source = probe_source(monkeypatch, lambda **kwargs: ready_measurement(**change))
    result = barrier.wait_for_candidate_readiness(runtime_for(source), timeout_seconds=.5)
    assert not result['ok'] and not result['ready']


@pytest.mark.parametrize('timeout', [False,0,-1,float('nan'),float('inf'),'120',10**500])
def test_invalid_wait_budget_fails_without_source_or_probe_access(clock, timeout):
    result = barrier.wait_for_candidate_readiness(object(), timeout_seconds=timeout)
    assert not result['ok'] and result['attempt_count'] == 0
    assert result['blocked_reason'] == 'candidate_readiness_timeout_invalid'


def test_structural_recheck_uses_original_measurement_time_not_replay_wall_clock(clock, monkeypatch):
    source = probe_source(monkeypatch, lambda **kwargs:ready_measurement())
    result = barrier.wait_for_candidate_readiness(runtime_for(source))
    assert barrier.candidate_readiness_barrier_ok(result, postgres=True)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    delta = old - datetime.fromisoformat(result['started_at'])
    for key in ('started_at','completed_at'):
        result[key] = (datetime.fromisoformat(result[key]) + delta).isoformat()
    result['final_measurement']['observed_at'] = (datetime.fromisoformat(result['final_measurement']['observed_at'])+delta).isoformat()
    assert barrier.candidate_readiness_barrier_ok(result, postgres=True)
    bad = deepcopy(result);bad['applicable'] = False
    assert not barrier.candidate_readiness_barrier_ok(bad, postgres=True)
