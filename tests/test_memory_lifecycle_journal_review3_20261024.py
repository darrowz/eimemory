"""Same-payload lifecycle transitions must remain durable observations."""
from contextlib import closing

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('write_path', ['rewrite', 'append', 'atomic'])
@pytest.mark.parametrize('initial_status', ['active', 'removed'])
@pytest.mark.parametrize('replace_database', [False, True])
def test_returning_to_an_earlier_lifecycle_state_survives_recovery(
        tmp_path, write_path, initial_status, replace_database):
    with closing(RuntimeStore(tmp_path)) as store:
        original = store.append(RecordEnvelope.create(
            kind='memory', status=initial_status,
            title='Reviewed fixture memory lifecycle',
            summary='A withdrawn memory stays withdrawn after rebuilding its authority.',
            content={'text': 'Use the current authoritative lifecycle for recall.'},
            scope=ScopeRef('review3', 'audit', 'lifecycle', 'owner'),
            source='audit.fixture', source_id='review3-fixture'))

        def write(status):
            updated = RecordEnvelope.from_dict(original.to_dict())
            updated.status = status
            if write_path == 'atomic':
                def mutation(sqlite):
                    sqlite.upsert(updated, commit=False)
                    return updated, [updated], []
                return store.mutate_records_atomically(mutation)
            return getattr(store, write_path)(updated)

        write('removed' if initial_status == 'active' else 'active')
        write(initial_status)
        assert store.get_by_exact_ref(original.record_id, scope=original.scope,
                                      source_id=original.source_id).status == initial_status
        rebuilt = store.rebuild_sqlite_from_jsonl(replace=replace_database)
        assert rebuilt['ok'], rebuilt
        restored = store.get_by_exact_ref(original.record_id, scope=original.scope,
                                         source_id=original.source_id)
        assert restored.status == initial_status


def memory():
    return RecordEnvelope.create(kind='memory', status='removed',
        title='Reviewed withdrawal fixture',
        summary='A temporarily restored memory remains withdrawn after the final decision.',
        content={'text': 'Use the current authoritative lifecycle for recall.'},
        scope=ScopeRef('review3', 'audit', 'lifecycle', 'owner'),
        source='audit.fixture', source_id='review3-fixture')


@pytest.mark.parametrize('replace_database', [False, True])
def test_independent_writers_record_distinct_withdrawal_observations(tmp_path, replace_database):
    with closing(RuntimeStore(tmp_path)) as first, closing(RuntimeStore(tmp_path)) as second:
        withdrawn = first.append(memory())
        restored = RecordEnvelope.from_dict(withdrawn.to_dict())
        restored.status = 'active'
        second.rewrite(restored)
        first.rewrite(withdrawn)
        assert second.rebuild_sqlite_from_jsonl(replace=replace_database)['ok']
        current = second.get_by_exact_ref(withdrawn.record_id, scope=withdrawn.scope,
                                          source_id=withdrawn.source_id)
        assert current.status == 'removed'


@pytest.mark.parametrize('append_before_failure', [False, True])
def test_export_retry_keeps_the_committed_observation_ids_and_final_withdrawal(
        tmp_path, monkeypatch, append_before_failure):
    with closing(RuntimeStore(tmp_path)) as writer:
        withdrawn = writer.append(memory())
        restored = RecordEnvelope.from_dict(withdrawn.to_dict())
        restored.status = 'active'
        native_append = writer.log.append_payload

        def unavailable(*args, **kwargs):
            if append_before_failure:
                native_append(*args, **kwargs)
            raise OSError('synthetic journal projection interruption')

        with monkeypatch.context() as fault:
            fault.setattr(writer.log, 'append_payload', unavailable)
            writer.rewrite(restored)
            writer.rewrite(withdrawn)
        with writer.locked() as sqlite:
            pending = sqlite.pending_exports(limit=10)
        assert len(pending) == 2
        expected_ids = {item['operation_id'] for item in pending}
    with closing(RuntimeStore(tmp_path)) as recovery:
        assert recovery.flush_exports()['remaining'] == 0
        exported_ids = {entry.operation_id for entry in recovery.log.scan_strict()}
        assert expected_ids <= exported_ids
        assert recovery.rebuild_sqlite_from_jsonl(replace=True)['ok']
        assert recovery.get_by_exact_ref(withdrawn.record_id, scope=withdrawn.scope,
                                         source_id=withdrawn.source_id).status == 'removed'


def test_insert_once_business_retry_keeps_its_original_record_export(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        record = memory()
        first = store.append(record, existing_match=lambda current: current.to_dict() == record.to_dict())
        entries = [(item.operation_id, item.payload_digest) for item in store.log.scan_strict()]
        second = store.append(record, existing_match=lambda current: current.to_dict() == record.to_dict())
        assert first.to_dict() == second.to_dict()
        assert [(item.operation_id, item.payload_digest) for item in store.log.scan_strict()] == entries
