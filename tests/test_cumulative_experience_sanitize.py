"""Bounded synthetic regressions for the experience sanitizer owner."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.experience import record_experience_item, record_outcome_trace, record_skill_trace
from eimemory.experience.sanitize import OutcomeSanitizationError, sanitize_outcome_payload


def _runtime_and_writes():
    writes = []
    def append(record):
        writes.append(record)
        return record
    return SimpleNamespace(store=SimpleNamespace(append=append)), writes


def _skill_payload(extra):
    return {
        'trace_id': 'synthetic-trace', 'task_type': 'fixture', 'input_summary': 'fixture',
        'selected_skills': [], 'actions': [], 'outcome': 'success', 'feedback': {},
        'latency_ms': 1, 'extra': extra,
    }


def _assert_public_rejection(extra):
    runtime, writes = _runtime_and_writes()
    item = record_experience_item(runtime, {'experience_kind': 'fixture', 'extra': extra})
    skill = record_skill_trace(runtime, _skill_payload(extra))
    outcome = record_outcome_trace(runtime, {'trace_id': 'synthetic-trace', 'outcome': 'success', 'extra': extra})
    assert item == {'ok': False, 'error': 'sensitive_payload'}
    assert skill == {'ok': False, 'error': 'sensitive_payload'}
    assert outcome['ok'] is False and outcome['error'].startswith('unsafe payload: ')
    assert writes == []


@pytest.mark.parametrize('key', [
    'https://fixture-user:fixture-pass@fixture.invalid/path',
    'Bearer fixture-bearer-value',
    'data:image/png;base64,RklYVFVSRQ==',
    'A' * 512,
    'x' * 4097,
])
def test_dictionary_keys_share_value_safety_and_length_limits(key):
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload({key: 'synthetic'})
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload({'ordinary_key': key})
    _assert_public_rejection({key: 'synthetic'})


@pytest.mark.parametrize('payload', [
    {'note': 'https://[fixture'},
    {'https://[fixture': 'ordinary value'},
])
def test_malformed_url_becomes_structured_sanitization_failure(payload):
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload(payload)
    _assert_public_rejection(payload)


@dataclass
class _Node:
    child: object = None


@pytest.mark.parametrize('kind', ['dict', 'list', 'dataclass'])
def test_cyclic_structures_fail_before_unbounded_conversion(kind):
    if kind == 'dict':
        value = {}; value['child'] = value
    elif kind == 'list':
        value = []; value.append(value)
    else:
        value = _Node(); value.child = value
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload({'fixture': value})
    _assert_public_rejection(value)


def test_deep_plain_structure_fails_before_unbounded_bridge_conversion():
    value = {}
    leaf = value
    for _ in range(1100):
        leaf['next'] = {}; leaf = leaf['next']
    _assert_public_rejection(value)


def _nested(depth):
    value = 'leaf'
    for _ in range(depth):
        value = {'child': value}
    return value


def test_existing_depth_boundary_and_shared_noncyclic_references():
    assert sanitize_outcome_payload(_nested(5)) == _nested(5)
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload(_nested(6))
    shared = {'value': ['synthetic']}
    assert sanitize_outcome_payload({'left': shared, 'right': shared}) == {
        'left': {'value': ['synthetic']}, 'right': {'value': ['synthetic']},
    }


class _TextValue:
    def __str__(self):
        return 'synthetic-custom-value'


@dataclass
class _AllowedTypes:
    path: Path
    day: date
    instant: datetime
    pair: tuple
    items: set
    other: object


def test_supported_types_keep_their_existing_json_safe_representations():
    instant = datetime(2026, 10, 2, tzinfo=timezone.utc)
    value = _AllowedTypes(Path('fixture/relative'), date(2026, 10, 2), instant, (1, None), {'b', 'a'}, _TextValue())
    expected = {
        'path': 'fixture/relative', 'day': '2026-10-02', 'instant': instant.isoformat(),
        'pair': [1, None], 'items': ['a', 'b'], 'other': 'synthetic-custom-value',
    }
    assert sanitize_outcome_payload({'fixture': value}) == {'fixture': expected}
    runtime, writes = _runtime_and_writes()
    result = record_experience_item(runtime, {'experience_kind': 'fixture', 'extra': value})
    assert result['ok'] is True and len(writes) == 1
    assert writes[0].content['extra'] == expected


def test_ordinary_keys_are_preserved_and_counter_exception_remains_narrow():
    payload = {' ordinary key ': 'fixture', 7: 'numeric key', 'usage': {'token_count': 0}}
    assert sanitize_outcome_payload(payload) == {' ordinary key ': 'fixture', '7': 'numeric key', 'usage': {'token_count': 0}}
    for value in (True, -1, 'synthetic'):
        with pytest.raises(OutcomeSanitizationError):
            sanitize_outcome_payload({'usage': {'token_count': value}})
