"""Committed record moves and withdrawals survive export failure and replay."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier, Event

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef('review2', 'fixture', 'workspace', 'owner-a')


def seed(store, suffix='original'):
    return store.append(RecordEnvelope.create(
        kind='memory', title='Synthetic recovery ' + suffix,
        content={'text': 'Only the current exact owner retains this memory.'},
        scope=SCOPE, source='audit.fixture', source_id='fixture-source'))


def read(store, record, scope):
    return store.get_by_exact_ref(record.record_id, scope=scope, source_id=record.source_id)


def move(store, record, scope, *, atomic=False):
    updated = RecordEnvelope.from_dict(record.to_dict())
    updated.scope = scope
    if atomic:
        def apply(sqlite):
            sqlite.rewrite(updated, previous_scope=record.scope, commit=False)
            return updated, [updated], []
        return store.mutate_records_atomically(apply)
    return store.rewrite(updated, previous_scope=record.scope)


@pytest.mark.parametrize('atomic', [False, True])
@pytest.mark.parametrize('replace_database', [False, True])
def test_scope_move_round_trips_do_not_resurrect_retired_owners(tmp_path, atomic, replace_database):
    with closing(RuntimeStore(tmp_path)) as store:
        original = current = seed(store)
        owner_b = replace(SCOPE, user_id='owner-b')
        owner_c = replace(SCOPE, user_id='owner-c')
        # Repeated identical A -> B payloads are distinct lifecycle events.
        # Neither unchanged record timestamps nor payload digest dedupe may
        # erase the final move after an intervening return to A.
        for scope in (owner_b, SCOPE, owner_b, owner_c):
            previous_scope = current.scope
            current = move(store, current, scope, atomic=atomic)
            assert read(store, current, previous_scope) is None
        report = store.rebuild_sqlite_from_jsonl(replace=replace_database)
        assert report['ok'], report
        assert read(store, original, owner_c) is not None
        assert read(store, original, SCOPE) is None
        assert read(store, original, owner_b) is None


def test_selective_export_cannot_replay_older_pending_snapshot_after_move(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        native_flush = store._flush_committed_exports
        monkeypatch.setattr(store, '_flush_committed_exports', lambda *_args: None)
        original = seed(store)
        # Exceed the post-commit newest-100 opportunistic export window.
        for index in range(101):
            seed(store, f'distractor-{index}')
        moved = move(store, original, replace(SCOPE, user_id='owner-b'))
        monkeypatch.setattr(store, '_flush_committed_exports', native_flush)
        native_flush()
        while store.flush_exports()['remaining']:
            pass
        report = store.rebuild_sqlite_from_jsonl(replace=True)
        assert report['ok'], report
        assert read(store, original, SCOPE) is None
        assert read(store, original, moved.scope) is not None


def test_move_retry_keeps_one_durable_move_operation(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        moved = move(store, original, replace(SCOPE, user_id='owner-b'))
        first = [entry for entry in store.log.scan_strict() if '_record_scope_move' in entry.payload]
        assert len(first) == 1
        store.rewrite(moved, previous_scope=original.scope)
        repeated = [entry for entry in store.log.scan_strict() if '_record_scope_move' in entry.payload]
        assert [(entry.operation_id, entry.payload_digest) for entry in repeated] == [
            (entry.operation_id, entry.payload_digest) for entry in first]


def test_late_duplicate_move_export_cannot_retire_a_newer_incarnation(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        moved = move(store, original, replace(SCOPE, user_id='owner-b'))
        first_move = next(entry for entry in store.log.scan_strict() if '_record_scope_move' in entry.payload)
        move(store, moved, SCOPE)
        store.log.append_payload(first_move.payload, operation_id=first_move.operation_id)
        report = store.rebuild_sqlite_from_jsonl(replace=True)
        assert report['ok'], report
        assert read(store, original, SCOPE) is not None
        assert read(store, original, moved.scope) is None


@pytest.mark.parametrize('fault', ['schema', 'nonce', 'source', 'record_id', 'same_scope', 'scope_type'])
def test_invalid_scope_move_markers_fail_closed(tmp_path, fault):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        current = RecordEnvelope.from_dict(original.to_dict())
        current.scope = replace(SCOPE, user_id='owner-b')
        marker = {'schema': 'record_scope_move.v1', 'nonce': '1' * 32,
                  'previous_ref': {'record_id': original.record_id, 'source_id': original.source_id,
                                   'scope': asdict(original.scope)}}
        if fault == 'schema':
            marker['schema'] = 'unknown'
        elif fault == 'nonce':
            marker['nonce'] = ''
        elif fault == 'source':
            marker['previous_ref']['source_id'] = 'another-source'
        elif fault == 'record_id':
            marker['previous_ref']['record_id'] = 'mem_another_record'
        elif fault == 'same_scope':
            marker['previous_ref']['scope'] = asdict(current.scope)
        else:
            marker['previous_ref']['scope']['user_id'] = []
        store.log.append_payload({**current.to_dict(), '_record_scope_move': marker})
        report = store.rebuild_sqlite_from_jsonl(replace=True)
        assert report['ok'] is False
        assert report['replaced'] is False
        assert read(store, original, SCOPE).status == 'active'
        assert read(store, original, current.scope) is None


@pytest.mark.parametrize('replace_database', [False, True])
def test_scope_move_replay_rejects_a_conflicting_retired_source(tmp_path, replace_database):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        current = RecordEnvelope.from_dict(original.to_dict())
        current.scope = replace(SCOPE, user_id='owner-b')
        current.source_id = 'another-source'
        marker = {'schema': 'record_scope_move.v1', 'nonce': '1' * 32,
                  'previous_ref': {'record_id': original.record_id, 'source_id': current.source_id,
                                   'scope': asdict(original.scope)}}
        # Internally consistent event, but it claims an old identity owned by
        # another source. The real move transaction could never produce it.
        store.log.append_payload({**current.to_dict(), '_record_scope_move': marker})
        report = store.rebuild_sqlite_from_jsonl(replace=replace_database)
        assert report['ok'] is False
        assert report['replaced'] is False
        assert read(store, original, SCOPE).to_dict() == original.to_dict()
        assert read(store, current, current.scope) is None


@pytest.mark.parametrize('failure', ['jsonl', 'markdown'])
def test_committed_withdrawal_returns_success_when_derived_export_fails(tmp_path, monkeypatch, failure):
    with closing(RuntimeStore(tmp_path)) as store:
        record = seed(store)
        def fail(*_args, **_kwargs):
            raise OSError('synthetic derived export unavailable')
        with monkeypatch.context() as fault:
            if failure == 'jsonl':
                fault.setattr(store.log, 'append_payload', fail)
            else:
                fault.setattr('eimemory.storage.runtime_store.export_record_markdown', fail)
            record.status = 'removed'
            result = store.rewrite(record)
        assert result.status == 'removed'
        assert read(store, record, record.scope).status == 'removed'
        assert store.flush_exports()['remaining'] == 0
        assert store.rebuild_sqlite_from_jsonl(replace=True)['ok']
        assert read(store, record, record.scope).status == 'removed'


def test_interrupted_rewrite_rolls_back_authority_and_outbox(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        record = seed(store)
        before = read(store, record, record.scope).to_dict()
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt('synthetic interruption before commit')
        monkeypatch.setattr(store, '_enqueue_record_exports', interrupt)
        record.status = 'removed'
        with pytest.raises(KeyboardInterrupt):
            store.rewrite(record)
        with store.locked() as sqlite:
            assert not sqlite.in_transaction
        assert read(store, record, record.scope).to_dict() == before


def test_stale_move_cannot_copy_a_record_from_an_already_retired_owner(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        moved = move(store, original, replace(SCOPE, user_id='owner-b'))
        with pytest.raises(ValueError, match='scope_move_source_missing'):
            move(store, original, replace(SCOPE, user_id='owner-c'))
        assert read(store, original, moved.scope) is not None
        assert read(store, original, replace(SCOPE, user_id='owner-c')) is None


def test_move_does_not_overwrite_an_occupied_destination(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        destination = replace(SCOPE, user_id='owner-b')
        other = RecordEnvelope.from_dict(original.to_dict())
        other.scope, other.summary = destination, 'Independent destination fact'
        store.append(other)
        with pytest.raises(ValueError, match='scope_move_target_exists'):
            move(store, original, destination)
        assert read(store, original, SCOPE) is not None
        assert read(store, original, destination).summary == other.summary


@pytest.mark.parametrize('same_target', [False, True])
def test_independent_writers_cannot_fork_one_scope_move(tmp_path, same_target):
    with closing(RuntimeStore(tmp_path)) as first, closing(RuntimeStore(tmp_path)) as second:
        original = seed(first)
        barrier = Barrier(2)
        def execute(index):
            store = (first, second)[index]
            destination = replace(SCOPE, user_id='owner-b' if same_target or index == 0 else 'owner-c')
            barrier.wait(timeout=5)
            try:
                return move(store, original, destination).scope
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, (0, 1)))
        assert sum(isinstance(result, ScopeRef) for result in results) == (2 if same_target else 1)
        if not same_target:
            assert 'scope_move_source_missing' in results
        assert read(first, original, SCOPE) is None
        records = first.list_records(kinds=['memory'], status=None)
        assert len(records) == 1
        assert first.rebuild_sqlite_from_jsonl(replace=True)['ok']
        assert len(first.list_records(kinds=['memory'], status=None)) == 1


def test_move_retires_old_markdown_and_delayed_export_preserves_move_back(tmp_path, monkeypatch):
    from eimemory.storage.record_export import _scope_partition, exported_records_dir
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        old_path = exported_records_dir(tmp_path) / _scope_partition(SCOPE) / (original.record_id + '.md')
        assert old_path.exists()
        moved = move(store, original, replace(SCOPE, user_id='owner-b'))
        assert not old_path.exists()
        new_path = exported_records_dir(tmp_path) / _scope_partition(moved.scope) / (original.record_id + '.md')
        assert new_path.exists()
        with monkeypatch.context() as pending:
            pending.setattr(store, '_flush_committed_exports', lambda *_args: None)
            restored = move(store, moved, SCOPE)
            moved_again = move(store, restored, moved.scope)
            move(store, moved_again, SCOPE)
        assert old_path.exists()
        assert store.flush_exports()['remaining'] == 0
        assert old_path.exists() and not new_path.exists()


def test_move_cleanup_cannot_delete_a_concurrently_published_new_identity(tmp_path, monkeypatch):
    import eimemory.storage.runtime_store as storage_module
    from eimemory.storage.record_export import _scope_partition, exported_records_dir
    with closing(RuntimeStore(tmp_path)) as first, closing(RuntimeStore(tmp_path)) as second:
        original = seed(first)
        with monkeypatch.context() as pending:
            pending.setattr(first, '_flush_committed_exports', lambda *_args: None)
            move(first, original, replace(SCOPE, user_id='owner-b'))
        replacement = RecordEnvelope.from_dict(original.to_dict())
        replacement.source_id, replacement.summary = 'new-owner-source', 'New current identity'
        cleanup_ready, writer_started, writer_completed = Event(), Event(), Event()
        native_export = storage_module.export_record_markdown

        def pause_cleanup(root, record):
            if record.scope == SCOPE and record.status == 'rejected':
                cleanup_ready.set()
                assert writer_started.wait(timeout=5)
                # Without the transaction the other connection publishes here.
                # With the transaction it waits until old cleanup is committed.
                writer_completed.wait(timeout=1)
            return native_export(root, record)

        def publish_replacement():
            assert cleanup_ready.wait(timeout=5)
            writer_started.set()
            result = second.append(replacement)
            writer_completed.set()
            return result

        monkeypatch.setattr(storage_module, 'export_record_markdown', pause_cleanup)
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(publish_replacement)
            assert first.flush_exports()['exported'] >= 1
            publishing.result(timeout=5)
        assert first.flush_exports()['remaining'] == 0
        assert read(first, replacement, SCOPE).to_dict() == replacement.to_dict()
        old_path = exported_records_dir(tmp_path) / _scope_partition(SCOPE) / (replacement.record_id + '.md')
        assert old_path.exists()
        assert replacement.summary in old_path.read_text()


def test_legacy_unmarked_snapshot_replay_keeps_original_format(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        original = seed(store)
        assert all('_record_scope_move' not in entry.payload for entry in store.log.scan_strict())
        assert store.rebuild_sqlite_from_jsonl(replace=True)['ok']
        assert read(store, original, SCOPE).to_dict() == original.to_dict()
