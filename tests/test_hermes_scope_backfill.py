from dataclasses import replace
import threading
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.sqlite_store import SqliteRecordStore
from deploy.backfill_hermes_scope import backfill


@pytest.fixture
def store(tmp_path):
    obj = SqliteRecordStore(tmp_path / 'state.sqlite')
    lock = threading.RLock()
    obj.bind_runtime_lock(lock)
    with lock:
        yield obj
    obj.conn.close()


def row(user='owner', tenant='tenant', status='active'):
    return replace(RecordEnvelope.create(kind='memory', status=status,
                          title='Verified preference', content={'text': 'Read the full document.'},
                          source='hermes.memory', source_id='hermes',
                          scope=ScopeRef(tenant_id=tenant, agent_id='default',
                                         workspace_id='hermes::channel::hermes', user_id=user)), record_id='mem_original')


def test_backfill_preserves_original_and_is_idempotent(store):
    original = row(); store.upsert(original)
    plan = backfill(store, tenant='tenant', user='owner')
    assert plan['planned'] == 1 and plan['written'] == 0
    result = backfill(store, tenant='tenant', user='owner', apply=True, expected_digest=plan['digest'])
    assert result['written'] == 1
    copied = store.get_by_id(original.record_id, scope=replace(original.scope, agent_id='hongtu', workspace_id='embodied::channel::hermes'))
    assert copied.content == original.content and copied.source_id == original.source_id
    assert store.get_by_id(original.record_id, scope=original.scope).to_dict() == original.to_dict()
    assert backfill(store, tenant='tenant', user='owner')['planned'] == 0


def test_does_not_copy_other_users_tenants_or_inactive_records(store):
    for r in [row('other'), row(tenant='other'), row(status='superseded')]:
        store.upsert(r)
    assert backfill(store, tenant='tenant', user='owner')['planned'] == 0


def test_changed_plan_and_conflicting_target_fail_without_writes(store):
    original = row(); store.upsert(original)
    with pytest.raises(ValueError, match='plan_changed'):
        backfill(store, tenant='tenant', user='owner', apply=True, expected_digest='wrong')
    target = replace(original, scope=replace(original.scope, agent_id='hongtu', workspace_id='embodied::channel::hermes'), content={'text':'different'})
    store.upsert(target)
    with pytest.raises(ValueError, match='target_conflict'):
        backfill(store, tenant='tenant', user='owner')
    assert store.get_by_id(original.record_id, scope=target.scope).content == {'text':'different'}


def test_partial_failure_rolls_back_entire_backfill(store, monkeypatch):
    original = row(); store.upsert(original)
    second = replace(original, record_id='mem_second'); store.upsert(second)
    plan = backfill(store, tenant='tenant', user='owner')
    original_upsert = store.upsert
    calls = []
    def fail_second(record, **kwargs):
        calls.append(record.record_id)
        if len(calls) == 2:
            raise OSError('fixture write failure')
        return original_upsert(record, **kwargs)
    monkeypatch.setattr(store, 'upsert', fail_second)
    with pytest.raises(OSError):
        backfill(store, tenant='tenant', user='owner', apply=True, expected_digest=plan['digest'])
    target = replace(original.scope, agent_id='hongtu', workspace_id='embodied::channel::hermes')
    assert store.get_by_id(original.record_id, scope=target) is None
    assert store.get_by_id(second.record_id, scope=target) is None
    assert not store.conn.in_transaction


def test_derived_and_non_hermes_sources_are_not_copied(store):
    store.upsert(replace(row(), source='loop', source_id='default'))
    assert backfill(store, tenant='tenant', user='owner')['planned'] == 0


def test_backfill_release_domains_are_explicit():
    from eimemory.governance.release_impact import _domains_for_change
    from pathlib import Path
    assert _domains_for_change(Path.cwd(), path='deploy/backfill_hermes_scope.py', ancestor='HEAD', current='HEAD') >= {'memory.recall','storage.integrity'}
