"""Public pack import keeps concurrent durable decisions and retryable work."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from threading import Barrier

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake import packs
from eimemory.models.records import RecordEnvelope, ScopeRef


SOURCE_SCOPE = ScopeRef('review3', 'source', 'pack', 'owner')
TARGET_SCOPE = ScopeRef('review3', 'target', 'pack', 'owner')


def _pack(tmp_path, *, count=1):
    records = []
    with Runtime.create(root=tmp_path / 'source') as source:
        for index in range(count):
            record = RecordEnvelope.create(
                kind='memory', title=f'Portable review evidence {index}',
                content={'text': f'Imported memory {index} preserves an independently reviewed durable decision.'},
                scope=SOURCE_SCOPE, source='review3.pack.fixture')
            records.append(source.store.append(record))
        source.export_knowledge_pack(tmp_path / 'pack', scope=asdict(SOURCE_SCOPE))
    return records


def test_pack_import_does_not_overwrite_a_concurrent_rejection(tmp_path, monkeypatch):
    exported = _pack(tmp_path)[0]
    root = tmp_path / 'target'
    with Runtime.create(root=root) as target, Runtime.create(root=root) as reviewer:
        replacement = RecordEnvelope.from_dict(exported.to_dict())
        replacement.scope = TARGET_SCOPE
        replacement.status = 'rejected'
        replacement.meta['review_reason'] = 'Concurrent operator withdrawal'
        native_screen = packs.screen_record_payload
        saved = []

        def reviewed_during_screen(record):
            if not saved:
                saved.append(reviewer.store.append(replacement).to_dict())
            return native_screen(record)

        monkeypatch.setattr(packs, 'screen_record_payload', reviewed_during_screen)
        report = target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))
        current = target.store.get_by_exact_ref(exported.record_id, scope=TARGET_SCOPE,
                                               source_id=exported.source_id)
        assert current.to_dict() == saved[0]
        assert report['written_count'] == 0
        assert report['skipped_existing_count'] == report['collision_count'] == 1
        assert report['collisions'] == [exported.record_id]


def test_two_pack_importers_count_only_the_committed_insert(tmp_path, monkeypatch):
    exported = _pack(tmp_path)[0]
    root = tmp_path / 'target'
    barrier = Barrier(2)
    native_screen = packs.screen_record_payload

    def screened_together(record):
        result = native_screen(record)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(packs, 'screen_record_payload', screened_together)

    def import_once():
        with Runtime.create(root=root) as target:
            return target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))

    # Initialize the shared schema before racing only the two imports.
    with Runtime.create(root=root):
        pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(import_once), pool.submit(import_once)
        reports = [first.result(timeout=20), second.result(timeout=20)]
    assert sorted(report['written_count'] for report in reports) == [0, 1]
    assert sum(report['skipped_existing_count'] for report in reports) == 1
    with Runtime.create(root=root) as target:
        assert target.store.count_records(kinds=['memory'], scope=TARGET_SCOPE) == 1
        assert target.store.get_by_exact_ref(exported.record_id, scope=TARGET_SCOPE,
                                             source_id=exported.source_id) is not None


def test_pack_with_visible_shared_id_reports_the_skipped_duplicate(tmp_path):
    with Runtime.create(root=tmp_path / 'source') as source:
        owned = RecordEnvelope.create(kind='memory', title='Owned pack record',
            content={'text': 'The owned record preserves a reusable source decision.'},
            scope=SOURCE_SCOPE, source='review3.pack.fixture')
        source.store.append(owned)
        shared = RecordEnvelope.from_dict(owned.to_dict())
        shared.scope = replace(SOURCE_SCOPE, user_id='')
        shared.content['text'] = 'The shared record contains a different reusable source decision.'
        source.store.append(shared)
        exported = source.export_knowledge_pack(tmp_path / 'pack', scope=asdict(SOURCE_SCOPE))
        assert exported['record_count'] == 2
    with Runtime.create(root=tmp_path / 'target') as target:
        report = target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))
        assert report['written_count'] == 1
        assert report['skipped_existing_count'] == report['collision_count'] == 1
        assert target.store.count_records(kinds=['memory'], scope=TARGET_SCOPE) == 1


def test_pack_write_failure_keeps_committed_prefix_retryable(tmp_path, monkeypatch):
    _pack(tmp_path, count=3)
    exported = [json.loads(line) for line in (tmp_path / 'pack' / 'records.jsonl').read_text().splitlines()]
    with Runtime.create(root=tmp_path / 'target') as target:
        native_upsert = target.store.sqlite.upsert
        calls = []

        def fail_second(record, **kwargs):
            calls.append(record.record_id)
            if len(calls) == 2:
                raise OSError('Synthetic interrupted pack write')
            return native_upsert(record, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(target.store.sqlite, 'upsert', fail_second)
            with pytest.raises(OSError, match='Synthetic interrupted pack write'):
                target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))
        existing = target.store.list_records(kinds=['memory'], scope=TARGET_SCOPE, limit=10)
        assert [record.record_id for record in existing] == [exported[0]['record_id']]
        recovered = target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))
        assert recovered['written_count'] == 2
        assert recovered['skipped_existing_count'] == 1
        replay = target.import_knowledge_pack(tmp_path / 'pack', scope=asdict(TARGET_SCOPE))
        assert replay['written_count'] == 0
        assert replay['skipped_existing_count'] == 3
        assert target.store.count_records(kinds=['memory'], scope=TARGET_SCOPE) == 3
