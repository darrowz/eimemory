"""Wait for the configured native vector projection without creating work.

Each observation uses the source's existing private vector SELECT and atomic
authority fences. Waiting never embeds, syncs an index, reruns a recall case,
or changes the caller's recall budget or its historical query-validity proof.
"""
from collections import Counter
from datetime import datetime, timezone
from math import isfinite
from time import monotonic, sleep

from eimemory.retrieval.postgres_vector import (PostgresVectorCandidateSource,
    PostgresVectorConfig, _CANDIDATE_ERROR_CODES)
from eimemory.retrieval.readiness_contract import MAX_AGE_SECONDS, readiness_ok, sanitize_readiness
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource

SCHEMA = 'candidate_readiness_barrier.v1'
MAX_WAIT_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = .2
POLL_SECONDS = .5
MAX_ATTEMPTS = 512
_BARRIER_CODES = frozenset({'readiness_probe_unavailable', 'readiness_contract_invalid',
    'readiness_probe_deadline_exceeded', 'candidate_readiness_deadline_exhausted',
    'candidate_source_unavailable', 'candidate_source_unsupported', 'candidate_source_changed',
    'candidate_readiness_timeout_invalid', 'candidate_readiness_attempts_exhausted',
    'readiness_probe_in_progress', 'disabled', 'not_configured', 'invalid_vector_index_config'})


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _code(value):
    return value if isinstance(value, str) and value in _CANDIDATE_ERROR_CODES | _BARRIER_CODES else 'readiness_probe_unavailable'


def _explicit_pg_bypass(source):
    """Only the native source's explicit configuration can exempt projection."""
    if type(source) is not PostgresVectorCandidateSource:
        return '', {}
    config = getattr(source, 'config', None)
    if type(config) is not PostgresVectorConfig or type(config.enabled) is not bool:
        return '', {}
    configuration = dict(enabled=config.enabled, configured=config.configured)
    reason = 'disabled' if config.enabled is False else 'not_configured' if not config.configured else ''
    return reason, configuration


def _bypass_measurement_ok(value, *, reason, configuration, at):
    if (reason not in {'disabled', 'not_configured'} or not isinstance(configuration, dict)
            or set(configuration) != {'enabled', 'configured'}
            or any(type(item) is not bool for item in configuration.values())
            or reason != ('disabled' if configuration['enabled'] is False else
                          'not_configured' if configuration['configured'] is False else '')
            or not isinstance(value, dict) or value != sanitize_readiness(value)
            or value.get('schema') != 'postgres_candidate_readiness.v1'
            or value.get('measurement_origin') != 'native_readonly_vector_query'
            or value.get('enabled') is not configuration['enabled']
            or reason == 'not_configured' and value.get('configured') is not False
            or any(value.get(key) is not False for key in ('ready', 'index_verified',
                'backend_query_verified', 'authority_unchanged', 'fingerprints_match', 'revision_matches'))
            or value.get('error') != reason or value.get('last_error') != reason
            or value.get('watermark') != '' or value.get('authority_revision') != ''
            or value.get('lag_seconds') is not None):
        return False
    try:
        observed = datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00'))
        return observed.tzinfo is not None and -5 <= (at - observed).total_seconds() <= MAX_AGE_SECONDS
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def candidate_readiness_barrier_ok(value, *, postgres, source=None):
    """Pure recheck of a saved successful barrier at its original timestamp.

This structural check does not grant authority to dictionaries. CPA additionally
requires its signed native body, protected engine contract and exact sources.
"""
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('ok') is not True:
        return False
    try:
        start = datetime.fromisoformat(value['started_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(value['completed_at'].replace('Z', '+00:00'))
        elapsed, budget = value['elapsed_seconds'], value['timeout_seconds']
        count, errors = value['attempt_count'], value['error_code_counts']
        if (start.tzinfo is None or end.tzinfo is None or end < start
                or not _number(elapsed) or not 0 <= elapsed <= budget
                or not _number(budget) or not 0 < budget <= MAX_WAIT_SECONDS
                or value.get('probe_timeout_seconds') != PROBE_TIMEOUT_SECONDS
                or type(count) is not int or not 0 <= count <= MAX_ATTEMPTS
                or not isinstance(errors, dict)
                or any(_code(k) != k or type(v) is not int or not 0 < v <= MAX_ATTEMPTS for k, v in errors.items())
                or value.get('blocked_reason') != ''):
            return False
        if not postgres:
            return (value.get('source_type') == 'sqlite' and value.get('status') == 'not_applicable'
                and value.get('applicable') is False and value.get('ready') is False
                and count == 0 and errors == {} and value.get('final_measurement') == {})
        if value.get('status') == 'not_applicable':
            reason, configuration = value.get('bypass_reason'), value.get('source_configuration')
            return (value.get('source_type') == 'postgres_vector' and value.get('applicable') is False
                and value.get('ready') is False and count == 1 and errors == {}
                and _bypass_measurement_ok(value.get('final_measurement'), reason=reason,
                    configuration=configuration, at=end)
                and (source is None or _explicit_pg_bypass(source) == (reason, configuration)))
        return (value.get('source_type') == 'postgres_vector' and value.get('status') == 'ready'
            and value.get('applicable') is True and value.get('ready') is True and count > 0
            and sum(errors.values()) == count - 1
            and readiness_ok(value.get('final_measurement'), at=end))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def wait_for_candidate_readiness(runtime, timeout_seconds=120):
    """Boundedly observe the same trusted source; never start repair or recall."""
    started = monotonic()
    result = dict(schema=SCHEMA, ok=False, applicable=True, ready=False, status='failed',
        blocked_reason='', source_type='unknown', started_at=_stamp(), completed_at='',
        elapsed_seconds=0.0, timeout_seconds=0.0, probe_timeout_seconds=PROBE_TIMEOUT_SECONDS,
        attempt_count=0, error_code_counts={}, final_measurement={})
    errors = Counter()

    def finish(reason='', *, ok=False, status='failed'):
        result.update(ok=ok, status=status, blocked_reason=reason, completed_at=_stamp(),
            elapsed_seconds=max(0.0, monotonic() - started), error_code_counts=dict(sorted(errors.items())))
        return result

    if not _number(timeout_seconds) or timeout_seconds <= 0:
        return finish('candidate_readiness_timeout_invalid')
    budget = min(float(timeout_seconds), MAX_WAIT_SECONDS)
    result['timeout_seconds'] = budget
    deadline = started + budget
    try:
        engine = runtime.memory.recall_engine
        source = engine.candidate_source
        construction_error = getattr(runtime, 'candidate_source_error', '')
    except Exception:
        return finish('candidate_source_unavailable')
    # Only a known native SQLite source can establish that PG is inapplicable.
    # Unknown adapters and PG subclasses missing the method fail closed.
    if type(source) is SQLiteCandidateSource:
        if construction_error:
            return finish('candidate_source_unavailable')
        result.update(applicable=False, source_type='sqlite')
        return finish(ok=True, status='not_applicable')
    if not isinstance(source, PostgresVectorCandidateSource):
        return finish('candidate_source_unsupported')
    result['source_type'] = 'postgres_vector'
    bypass_reason, configuration = _explicit_pg_bypass(source)
    source_config = getattr(source, 'config', None)
    if bypass_reason and construction_error:
        return finish('candidate_source_unavailable')
    try:
        probe = getattr(source, 'readiness', None)
    except Exception:
        return finish('readiness_probe_unavailable')
    if not callable(probe):
        return finish('readiness_probe_unavailable')

    def same_source():
        try:
            return (getattr(getattr(runtime, 'memory', None), 'recall_engine', None) is engine
                and getattr(engine, 'candidate_source', None) is source
                and getattr(source, 'config', None) is source_config
                and (not bypass_reason or _explicit_pg_bypass(source) == (bypass_reason, configuration)))
        except Exception:
            return False

    while result['attempt_count'] < MAX_ATTEMPTS:
        if not same_source():
            return finish('candidate_source_changed')
        before = monotonic()
        remaining = deadline - before
        if remaining <= 0:
            return finish('candidate_readiness_deadline_exhausted', status='timeout')
        timeout = min(PROBE_TIMEOUT_SECONDS, remaining)
        result['attempt_count'] += 1
        measurement = None
        try:
            measurement = probe(force=True, timeout_seconds=timeout)
        except Exception:
            code = 'readiness_probe_unavailable'
        else:
            code = _code(measurement.get('error') or measurement.get('last_error')) if isinstance(measurement, dict) else 'readiness_contract_invalid'
        after = monotonic()
        result['final_measurement'] = sanitize_readiness(measurement)
        if not same_source():
            errors['candidate_source_changed'] += 1
            return finish('candidate_source_changed')
        # A response arriving after either deadline cannot qualify this barrier.
        if after >= deadline or after - before > timeout:
            errors['readiness_probe_deadline_exceeded'] += 1
            if after >= deadline:
                return finish('candidate_readiness_deadline_exhausted', status='timeout')
        elif bypass_reason:
            # Explicit lexical operation remains valid, but a provider failure
            # or a ready-looking dictionary cannot claim this exemption.
            if _bypass_measurement_ok(measurement, reason=bypass_reason,
                    configuration=configuration, at=datetime.now(timezone.utc)):
                result.update(applicable=False, bypass_reason=bypass_reason,
                    source_configuration=configuration)
                return finish(ok=True, status='not_applicable')
            errors[code] += 1
            return finish('readiness_contract_invalid')
        elif readiness_ok(measurement):
            result['ready'] = True
            return finish(ok=True, status='ready')
        else:
            errors[code if code != 'readiness_probe_unavailable' or not isinstance(measurement, dict)
                   or measurement.get('error') or measurement.get('last_error') else 'readiness_contract_invalid'] += 1
        remaining = deadline - monotonic()
        if remaining <= 0:
            return finish('candidate_readiness_deadline_exhausted', status='timeout')
        sleep(min(POLL_SECONDS, remaining))
    return finish('candidate_readiness_attempts_exhausted', status='timeout')
