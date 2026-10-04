"""Synthetic v1/v2 identity compatibility; no adapter/provider execution."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from eimemory.experience import record_outcome_trace
from eimemory.experience.outcome import build_outcome_trace_record
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
import eimemory.evaluation.task_replay as replay

SCOPE = ScopeRef(tenant_id='fixture-tenant', agent_id='fixture-agent', workspace_id='fixture-workspace', user_id='fixture-user')


def _payload(trace='fixture-trace', key='', **extras):
    value = {'trace_id': trace, 'task_type': 'fixture.task', 'input_summary': 'synthetic original',
             'outcome': {'status': 'success'}, 'verifier': {'passed': True},
             'recorded_at': '2026-10-02T00:00:00+00:00'}
    if key:
        value['idempotency_key'] = key
    value.update(extras)
    return value


def _legacy_id(payload, scope=SCOPE):
    # Frozen old algorithm, independent of the production helper under test.
    stable = json.dumps({'scope': asdict(scope), 'operation': 'outcome_trace',
                         'idempotency_key': payload.get('idempotency_key') or payload['trace_id']},
                        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'ref_' + sha256(stable.encode('utf-8')).hexdigest()[:32]


def _legacy_record(payload, scope=SCOPE):
    record = build_outcome_trace_record(payload, scope=scope).record
    record.record_id = _legacy_id(payload, scope)
    return record


@pytest.fixture
def runtime(tmp_path):
    store = RuntimeStore(tmp_path / 'runtime')
    try:
        yield SimpleNamespace(store=store)
    finally:
        store.close()


@pytest.mark.parametrize('reverse', [False, True])
def test_key_and_trace_namespaces_do_not_overwrite_each_other(runtime, reverse):
    values = [_payload('unrelated-trace', 'shared-value'), _payload('shared-value')]
    if reverse:
        values.reverse()
    first = record_outcome_trace(runtime, values[0], scope=SCOPE)
    original = runtime.store.get_by_id(first['record_id'], scope=SCOPE, exact_scope=True).to_dict()
    second = record_outcome_trace(runtime, values[1], scope=SCOPE)
    assert first['record_id'] != second['record_id']
    assert first['idempotent'] is False and second['idempotent'] is False
    assert runtime.store.get_by_id(first['record_id'], scope=SCOPE, exact_scope=True).to_dict() == original
    assert len(runtime.store.list_records(kinds=['reflection'], scope=SCOPE, limit=10)) == 2


@pytest.mark.parametrize('key', ['', 'fixture-key'])
def test_new_builder_always_uses_v2_even_with_legacy_marker(key):
    for marker in ({}, {'record_identity_schema': 'v1'}, {'identity_schema': 'legacy'}):
        payload = _payload('fixture-trace', key, **marker)
        record = build_outcome_trace_record(payload, scope=SCOPE).record
        assert record.record_id != _legacy_id(payload)
        assert record.record_id == build_outcome_trace_record(payload, scope=SCOPE).record.record_id


@pytest.mark.parametrize('key', ['', 'fixture-key'])
def test_old_key_and_trace_retries_survive_restart_without_migration(tmp_path, key):
    root = tmp_path / 'restart'
    original_payload = _payload('legacy-trace', key)
    store = RuntimeStore(root)
    try:
        legacy = store.append(_legacy_record(original_payload))
        original = legacy.to_dict()
    finally:
        store.close()
    store = RuntimeStore(root)
    try:
        runtime = SimpleNamespace(store=store)
        retries = [_payload('legacy-trace', 'new-alias-key', input_summary='retry changed')]
        if key:
            retries.append(_payload('changed-trace', key, input_summary='retry changed again'))
        else:
            retries.append(_payload('legacy-trace', input_summary='retry changed again'))
        for retry in retries:
            result = record_outcome_trace(runtime, retry, scope=SCOPE)
            assert result['idempotent'] is True and result['record_id'] == legacy.record_id
        assert store.get_by_id(legacy.record_id, scope=SCOPE, exact_scope=True).to_dict() == original
        assert len(store.list_records(kinds=['reflection'], scope=SCOPE, limit=10)) == 1
    finally:
        store.close()


@pytest.mark.parametrize('reverse', [False, True])
def test_mismatched_legacy_hash_is_preserved_and_new_event_gets_separate_id(runtime, reverse):
    values = [_payload('unrelated-trace', 'shared-value'), _payload('shared-value')]
    if reverse:
        values.reverse()
    legacy = runtime.store.append(_legacy_record(values[0]))
    original = legacy.to_dict()
    result = record_outcome_trace(runtime, values[1], scope=SCOPE)
    assert result['idempotent'] is False and result['record_id'] != legacy.record_id
    assert runtime.store.get_by_id(legacy.record_id, scope=SCOPE, exact_scope=True).to_dict() == original
    assert len(runtime.store.list_records(kinds=['reflection'], scope=SCOPE, limit=10)) == 2


@pytest.mark.parametrize('mismatch', ['scope', 'source', 'report_type'])
def test_unrelated_legacy_records_are_not_reused_or_overwritten(runtime, mismatch):
    payload = _payload('legacy-trace', 'legacy-key')
    source_scope = ScopeRef(**{**asdict(SCOPE), 'user_id': 'other-user'}) if mismatch == 'scope' else SCOPE
    legacy = _legacy_record(payload, source_scope)
    if mismatch == 'source':
        legacy.source = 'synthetic.other-source'
    elif mismatch == 'report_type':
        legacy.meta['report_type'] = 'synthetic-other-report'
        legacy.meta['business_meta']['report_type'] = 'synthetic-other-report'
        legacy.provenance['report_type'] = 'synthetic-other-report'
    runtime.store.append(legacy)
    original = legacy.to_dict()
    result = record_outcome_trace(runtime, payload, scope=SCOPE)
    assert result['idempotent'] is False and result['record_id'] != legacy.record_id
    assert runtime.store.get_by_id(legacy.record_id, scope=source_scope, exact_scope=True).to_dict() == original


@pytest.mark.parametrize('key', ['', 'legacy-key'])
def test_nonatomic_fallback_can_reuse_exact_legacy_id_without_history_scan(key):
    payload = _payload('legacy-trace', key)
    legacy = _legacy_record(payload)
    queried = []
    def get_by_id(record_id, *, scope):
        queried.append(record_id)
        return legacy if record_id == legacy.record_id else None
    def unexpected(*args, **kwargs):
        raise AssertionError('exact legacy match should not append or scan')
    store = SimpleNamespace(get_by_id=get_by_id, append=unexpected, list_records=unexpected)
    result = record_outcome_trace(SimpleNamespace(store=store), payload, scope=SCOPE)
    assert result['idempotent'] is True and result['record_id'] == legacy.record_id
    assert legacy.record_id in queried


def test_concurrent_alias_retries_still_write_once(runtime):
    barrier = Barrier(2)
    def record(index):
        barrier.wait(timeout=5)
        return record_outcome_trace(runtime, _payload(f'parallel-{index}', 'shared-key'), scope=SCOPE)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, range(2)))
    assert len({result['record_id'] for result in results}) == 1
    assert sorted(result['idempotent'] for result in results) == [False, True]
    assert len(runtime.store.list_records(kinds=['reflection'], scope=SCOPE, limit=10)) == 1


def _replay_record(version):
    # Deliberately untrusted terminal source: reaching this gate proves identity
    # compatibility without mocking or exercising real terminal/provider chains.
    payload = _payload('replay-trace', 'replay-key', source='synthetic.untrusted-terminal')
    return _legacy_record(payload) if version == 'v1' else build_outcome_trace_record(payload, scope=SCOPE).record


def _validate_record(record):
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_id=lambda *_args, **_kwargs: record))
    return replay.validate_real_replay_source(runtime, source_record_id=record.record_id, scope=SCOPE)


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_replay_identity_accepts_both_schemas_but_keeps_terminal_trust_gate(version):
    result = _validate_record(_replay_record(version))
    assert result['ok'] is False and result['reason'] == 'untrusted_terminal_source'


@pytest.mark.parametrize('version', ['v1', 'v2'])
@pytest.mark.parametrize('mismatch', ['id', 'content', 'provenance', 'time', 'metadata', 'business_metadata', 'scope'])
def test_replay_preserves_strict_record_integrity_checks(version, mismatch):
    record = deepcopy(_replay_record(version))
    if mismatch == 'id':
        record.record_id = 'ref_' + '0' * 32
    elif mismatch == 'content':
        record.content['diagnosis']['confidence'] = 0.123
    elif mismatch == 'provenance':
        record.provenance['trace_id'] = 'wrong-trace'
    elif mismatch == 'time':
        record.time.created_at = '2025-01-01T00:00:00+00:00'
    elif mismatch == 'metadata':
        record.meta['task_type'] = 'wrong.task'
    elif mismatch == 'business_metadata':
        record.meta['business_meta']['task_type'] = 'wrong.task'
    elif mismatch == 'scope':
        record.scope = ScopeRef(**{**asdict(SCOPE), 'user_id': 'other-user'})
    result = _validate_record(record)
    assert result['ok'] is False and result['reason'] == 'outcome_trace_identity_mismatch'

@pytest.mark.parametrize('lookup', ['qualified', 'paged'])
def test_fallback_rejects_wrong_kind_even_when_identity_metadata_matches(lookup):
    from eimemory.experience.outcome import _existing_outcome_record
    payload = _payload('fixture-trace', 'fixture-key')
    wrong = build_outcome_trace_record(payload, scope=SCOPE).record
    wrong.kind = 'memory'
    store = SimpleNamespace(get_by_id=lambda *args, **kwargs: None)
    if lookup == 'qualified':
        store.find_outcome_trace = lambda **kwargs: wrong
    else:
        store.list_records = lambda **kwargs: [wrong]
    assert _existing_outcome_record(SimpleNamespace(store=store), payload, scope=SCOPE) is None


def test_scope_identity_is_case_sensitive_and_uses_all_fields(runtime):
    first = record_outcome_trace(runtime, _payload(), scope=SCOPE)
    for field in asdict(SCOPE):
        changed = ScopeRef(**{**asdict(SCOPE), field: getattr(SCOPE, field).upper()})
        other = record_outcome_trace(runtime, _payload(), scope=changed)
        assert other['idempotent'] is False
        assert other['record_id'] != first['record_id']
    assert record_outcome_trace(runtime, _payload(), scope=SCOPE)['idempotent'] is True
