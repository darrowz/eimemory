from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from eimemory.adapters.eibrain.rpc_server import _candidate_source_health, build_health_payload
from eimemory.ops import controlled_stability as probe
from eimemory.version import __version__
from eimemory.retrieval.readiness_contract import readiness_ok, sanitize_readiness
from test_controlled_stability import (
    COMMIT, health_payload, readiness, samples, seal, configured_team_observation_contract,
)


def test_native_readiness_recovers_current_health_without_changing_last_failed_query(monkeypatch):
    from eimemory import runtime_identity
    value = health_payload()
    candidate = value['retrieval']['candidate_source']
    candidate.update(index_verified=False, query_valid=False, available=False,
                     last_error='authority_revision_changed', current_readiness=readiness())
    monkeypatch.setattr(runtime_identity, 'package_tree_digest', lambda path: 'f' * 64)
    monkeypatch.setattr(probe, '_authenticated_health_payload', lambda: value)
    current = probe._health(COMMIT)
    assert current['ok'] and current['postgres_fresh'] and current['postgres_error'] == ''
    assert current['last_query_valid'] is False
    assert current['last_query_error'] == candidate['last_error'] == 'authority_revision_changed'
    assert candidate['query_valid'] is False and candidate['available'] is False


@pytest.mark.parametrize('change', ['absent', 'schema', 'origin', 'ready', 'backend', 'revision', 'fingerprint', 'lag', 'stale'])
def test_historical_query_valid_cannot_override_missing_or_failed_current_native_proof(monkeypatch, change):
    from eimemory import runtime_identity
    value = health_payload(); candidate = value['retrieval']['candidate_source']
    candidate.update(available=True, query_valid=True, current_readiness=readiness())
    proof = candidate['current_readiness']
    if change == 'absent': candidate.pop('current_readiness')
    if change == 'schema': proof['schema'] = 'foreign'
    if change == 'origin': proof['measurement_origin'] = 'metadata_only'
    if change == 'ready': proof['ready'] = False
    if change == 'backend': proof['backend_query_verified'] = False
    if change == 'revision': proof['revision_matches'] = False
    if change == 'fingerprint': proof['fingerprints_match'] = False
    if change == 'lag': proof['lag_seconds'] = -1
    if change == 'stale': proof['observed_at'] = (datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()
    monkeypatch.setattr(runtime_identity, 'package_tree_digest', lambda path: 'f' * 64)
    monkeypatch.setattr(probe, '_authenticated_health_payload', lambda: value)
    assert not probe._health(COMMIT)['ok']


@pytest.mark.parametrize('field,value', [('lag_seconds', True), ('lag_seconds', float('nan')),
    ('lag_seconds', float('inf')), ('lag_seconds', -1), ('lag_seconds', 10**1000),
    ('schema', 'other'), ('measurement_origin', 'metadata_only'), ('authority_revision', True),
    ('watermark', 'private business body'), ('observed_at', '2026-10-18T00:00:00'),
    ('backend_query_verified', 1), ('last_error', 'postgresql://secret@internal/private')])
def test_rpc_projection_rejects_malformed_proof_and_never_exports_private_fields(field, value):
    raw = readiness(); raw[field] = value; raw['private_body'] = 'must-not-export'
    safe = sanitize_readiness(raw)
    assert safe['ready'] is False and not readiness_ok(safe)
    assert 'must-not-export' not in str(safe) and 'secret@' not in str(safe)


def test_rpc_runs_native_readiness_within_existing_fragment_budget_and_retains_old_fields():
    calls = []
    def measured(**kwargs):
        calls.append(kwargs)
        return {**readiness(), 'private_rows': ['secret business text']}
    source = SimpleNamespace(health=lambda: dict(enabled=True, configured=True, available=False,
        index_verified=False, query_valid=False, last_error='authority_revision_changed', circuit='closed'),
        readiness=measured)
    runtime = SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))
    result = _candidate_source_health(runtime)
    assert calls == [{'timeout_seconds': .20}] and result['current_readiness']['ready'] is True
    assert not result['query_valid'] and not result['available']
    assert result['last_error'] == 'authority_revision_changed' and 'secret business text' not in str(result)


def test_stalled_native_readiness_remains_bounded_and_single_flight(tmp_path):
    from eimemory.api.runtime import Runtime
    entered, release = Event(), Event(); calls = []
    def stalled(**kwargs):
        calls.append(kwargs); entered.set(); assert release.wait(5)
        return readiness()
    with Runtime.create(root=tmp_path) as runtime:
        runtime.memory.recall_engine.candidate_source = SimpleNamespace(
            health=lambda: {'enabled': True, 'configured': True}, readiness=stalled)
        try:
            started = monotonic()
            first = build_health_payload(runtime, listen_host='127.0.0.1', listen_port=1)
            assert entered.is_set() and monotonic()-started < 1
            second = build_health_payload(runtime, listen_host='127.0.0.1', listen_port=1)
            assert first['retrieval']['health_status'] == second['retrieval']['health_status'] == 'timeout'
            assert calls == [{'timeout_seconds': .20}]
        finally:
            release.set()


def test_rpc_cannot_reuse_old_ready_after_native_source_reports_backend_failure(tmp_path):
    from eimemory.api.runtime import Runtime
    from test_postgres_readiness_recovery import setup_source

    source, authority, provider, repository = setup_source()
    with Runtime.create(root=tmp_path) as runtime:
        runtime.memory.recall_engine.candidate_source = source
        first = build_health_payload(runtime, listen_host='127.0.0.1', listen_port=1)
        assert first['retrieval']['candidate_source']['current_readiness']['ready'] is True
        source._record_query_failure('postgres_timeout')
        repository.error = TimeoutError('actual backend failure')
        second = build_health_payload(runtime, listen_host='127.0.0.1', listen_port=1)
        current = second['retrieval']['candidate_source']
        assert current['current_readiness']['ready'] is False
        assert current['last_error'] == 'postgres_timeout' and current['query_valid'] is False
        assert len(repository.searches) == 2
        repository.error = None
        assert source.readiness(force=True)['ready']
        third = build_health_payload(runtime, listen_host='127.0.0.1', listen_port=1)
        assert third['retrieval']['candidate_source']['current_readiness']['ready'] is True
        assert third['retrieval']['candidate_source']['query_valid'] is False


@pytest.mark.parametrize('change', ['watermark', 'authority_revision', 'missing', 'false_backend', 'old_timestamp'])
def test_each_sample_requires_current_native_pg_and_vector_proof_same_generation(change):
    observations = samples(); last = observations[-1]
    if change in ('watermark', 'authority_revision'): last['postgres_snapshot'][change] = '999'
    if change == 'missing': last['health'].pop('postgres_readiness')
    if change == 'false_backend': last['health']['postgres_readiness']['backend_query_verified'] = False
    if change == 'old_timestamp':
        last['health']['postgres_readiness']['observed_at'] = (datetime.fromisoformat(last['observed_at'])-timedelta(seconds=31)).isoformat()
    seal(last)
    result = probe.assess_controlled_stability(observations, expected_commit=COMMIT, expected_version=__version__)
    assert not result['ok'] and 'sample_current_postgres_readiness_unverified' in result['blocked_reasons']


def test_current_release_cannot_downgrade_its_sample_to_an_old_health_model():
    observations = samples()
    for sample in observations:
        sample['health'].update(version='1.14.145')
        sample['health'].pop('postgres_readiness'); sample['health'].pop('postgres_health_model')
        seal(sample)
    assert not probe.assess_controlled_stability(observations, expected_commit=COMMIT, expected_version='1.14.146')['ok']
    # A caller explicitly verifying a genuine old receipt can still inspect it.
    assert probe.assess_controlled_stability(observations, expected_commit=COMMIT, expected_version='1.14.145')['ok']


def test_saved_current_proof_rechecks_at_its_sample_time_not_todays_wall_clock():
    observations = samples()
    for sample in observations:
        sample['observed_at'] = (datetime.fromisoformat(sample['observed_at'])-timedelta(hours=1)).isoformat()
        sample['health']['postgres_readiness']['observed_at'] = sample['observed_at']; seal(sample)
    assessment = datetime.fromisoformat(observations[-1]['observed_at'])+timedelta(seconds=1)
    first = probe.assess_controlled_stability(observations, expected_commit=COMMIT,
        expected_version=__version__, now=assessment)
    assert first['ok']
    assert first == probe.assess_controlled_stability(observations, expected_commit=COMMIT,
        expected_version=__version__, now=first['assessed_at'])
    assert not readiness_ok(observations[-1]['health']['postgres_readiness'])
