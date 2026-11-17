"""Independent parent-availability regressions over real SQLite hydration.

Only temporary synthetic stores are mutated. No reader raises a fabricated
exception; an existing corrupt row and an absent row take native hydration.
"""
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4
import json
import socket

import pytest

from test_ordinary_chat_temporal_semantics import chat
from test_chat_temporal_reader_boundary import owner, strip_marker
from eimemory.contracts.l1_source_order import L1SourceOrderBlocked
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.knowledge import chat_temporal as temporal
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope
from eimemory.storage.jsonl import canonical_payload_json, payload_digest


_EXACT = ('record_id=? AND tenant_id=? AND agent_id=? AND workspace_id=? '
          'AND user_id=? AND source_id=?')
_FAULTS = ('malformed_inline', 'projection_mismatch', 'missing')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail('No network in synthetic parent-availability regression')
    monkeypatch.setattr(socket, 'create_connection', prohibited)


def target(chat, managed):
    text = ("Since 2020-01-01, Willow's inspection zone is East."
            if managed else "Willow's inspection zone is East.")
    parent_id = chat.say(text)
    rows = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=['hermes.l1'], kinds=['memory'], limit=100)
    item = next(row for row in rows if business_metadata(row.meta).get(
        'source_event_binding', {}).get('record_id') == parent_id)
    if not managed:
        # A historical bound body, not a newly persisted unresolved marker.
        item = deepcopy(item)
        item.record_id = 'mem_' + uuid4().hex
        strip_marker(item)
        item.meta['semantic_key'] = 'parent-availability:' + item.record_id
        chat.runtime.store.append(item)
    parent = chat.runtime.store.get_by_exact_ref(parent_id, scope=owner(), source_id='hermes',
                                                fail_on_unavailable=True)
    return item, parent


def damage_parent(store, parent, fault):
    identity = (parent.record_id, parent.scope.tenant_id, parent.scope.agent_id,
                parent.scope.workspace_id, parent.scope.user_id, parent.source_id)
    def damage(sqlite):
        if fault == 'missing':
            sqlite.execute(f'DELETE FROM records WHERE {_EXACT}', identity)
        else:
            payload = parent.to_dict()
            if fault == 'malformed_inline':
                raw, digest = '{"broken":true}', ''
            else:
                payload['source_id'] = 'foreign-synthetic-source'
                raw, digest = canonical_payload_json(payload), payload_digest(payload)
            # Force the genuine inline path, independent of segment settings.
            sqlite.execute(f'UPDATE records SET payload_json=?, payload_digest=?, '
                           f'payload_pointer_json=? WHERE {_EXACT}', (raw, digest, '', *identity))
        return None, [], []
    store.mutate_records_atomically(damage)
    with store.locked() as sqlite:
        count = sqlite.execute(f'SELECT COUNT(*) FROM records WHERE {_EXACT}', identity).fetchone()[0]
    assert count == (0 if fault == 'missing' else 1)
    assert store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id) is None
    if fault == 'missing':
        assert store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id,
                                      fail_on_unavailable=True) is None
    else:
        with pytest.raises(RuntimeError, match='exact_record_unavailable'):
            store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id,
                                   fail_on_unavailable=True)


def reasons(chat, item):
    return temporal.current_target_temporal_reasons(chat.runtime.memory, [item], scope=owner(),
        valid_at=datetime.now(timezone.utc), deadline_at=perf_counter() + 5)


@pytest.mark.parametrize('managed', [False, True], ids=['bound-legacy', 'managed-family'])
@pytest.mark.parametrize('fault', _FAULTS)
def test_native_parent_absence_and_unreadability_remain_distinct(chat, managed, fault):
    item, parent = target(chat, managed)
    assert reasons(chat, item) == ['']
    damage_parent(chat.runtime.store, parent, fault)
    expected = ('source_temporal_inapplicable' if fault == 'missing'
                else 'source_current_authority_unavailable')
    assert reasons(chat, item) == [expected]


def ordinary(store, index):
    text = f'Inspection cabinet WLW-{index} uses a violet calibration card.'
    return store.append(RecordEnvelope.create(kind='memory', scope=owner(), source='hermes.memory',
        source_id='hermes', title=text, summary=text, content={'text': text, 'memory_type': 'fact'},
        meta={'memory_type': 'fact', 'memory_layer': 'l1', 'force_capture': True}))


@pytest.mark.parametrize('managed', [False, True], ids=['bound-legacy', 'managed-family'])
@pytest.mark.parametrize('fault', _FAULTS)
def test_generation_clears_valid_cases_on_unreadable_parent_but_skips_true_absence(chat, monkeypatch, managed, fault):
    item, parent = target(chat, managed)
    good = [ordinary(chat.runtime.store, index) for index in range(4)]
    # Count exceeds the normal minimum but remains below this batch's limit:
    # the bad target must be encountered, even after all valid predecessors.
    def build():
        return benchmark.build_controlled_memory_benchmark(chat.runtime, scope=owner(),
                                                           source_ids=['hermes'], limit=5)
    def prohibited(*args, **kwargs):
        pytest.fail('Dataset qualification must not trial-run recall')
    monkeypatch.setattr(chat.runtime.memory, 'recall', prohibited)
    before = build()
    assert {case['source_binding']['record_id'] for case in before['cases']} == {
        item.record_id, *(record.record_id for record in good)}
    damage_parent(chat.runtime.store, parent, fault)
    result = build()
    generation = result['dataset_generation']
    if fault == 'missing':
        assert generation['ok'] is True
        assert {case['source_binding']['record_id'] for case in result['cases']} == {
            record.record_id for record in good}
        assert generation['skipped_reason_counts']['source_temporal_inapplicable'] >= 1
    else:
        assert generation['ok'] is False
        assert generation['reason'] == 'source_current_authority_unavailable'
        assert result['cases'] == [], 'A previously admitted ordinary target cannot hide unknown authority'


@pytest.mark.parametrize('fault', _FAULTS)
def test_capture_admission_default_and_immutable_source_history_are_preserved(chat, fault):
    item, parent = target(chat, managed=True)
    access = temporal.access_for(chat.runtime.memory, owner(), 'hermes')
    assert access.admit_evidence(deepcopy(item)) is True
    before = json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False)
    assert reasons(chat, item) == ['']
    persisted = chat.runtime.store.get_by_exact_ref(item.record_id, scope=owner(), source_id='hermes',
                                                   fail_on_unavailable=True)
    assert json.dumps(persisted.to_dict(), sort_keys=True, ensure_ascii=False) == before
    # Capture retains its existing missing-parent rejection rather than the
    # stricter read-only RuntimeError classification used during qualification.
    damage_parent(chat.runtime.store, parent, fault)
    with pytest.raises(L1SourceOrderBlocked, match='l1_source_parent_unavailable'):
        access.admit_evidence(deepcopy(item))
