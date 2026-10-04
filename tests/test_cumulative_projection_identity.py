from dataclasses import asdict
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


def test_sqlite_status_repair_returns_exact_internal_reference():
    scope = ScopeRef(tenant_id='fake', user_id='user-a')
    record = RecordEnvelope.create(kind='memory', title='fake', scope=scope, source_id='partition-a')
    row = dict(record_id=record.record_id, kind=record.kind, status='active',
               source_id=record.source_id, **asdict(scope))
    sql = object.__new__(SqliteRecordStore)
    sql.conn = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(fetchall=lambda: [row])), commit=Mock())
    sql._record_from_storage_row = Mock(return_value=record)
    sql.upsert = Mock()
    result = sql.repair_status_projection_mismatches(scope=None, commit=False)
    assert result['repaired_record_refs'] == [dict(record_id=record.record_id, scope=asdict(scope), source_id='partition-a')]
    sql.upsert.assert_called_once_with(record, commit=False)
    sql.conn.commit.assert_not_called()


@pytest.mark.parametrize('mismatch', [False, True])
def test_runtime_projection_hydrates_exact_scope_and_source(tmp_path, monkeypatch, mismatch):
    scope = ScopeRef(tenant_id='fake', user_id='user-a')
    record = RecordEnvelope.create(kind='memory', title='fake', scope=scope, source_id='partition-a')
    ref = dict(record_id=record.record_id, scope=asdict(scope), source_id=record.source_id)
    report = dict(schema='record_status_projection_repair.v1', ok=True,
                  repaired_count=1, repaired_record_ids=[record.record_id], repaired_record_refs=[ref])
    returned = RecordEnvelope.from_dict(record.to_dict())
    if mismatch:
        returned.source_id = 'partition-b'
    store = object.__new__(RuntimeStore)
    store.root = tmp_path
    store._lock = RLock()
    store.sqlite = SimpleNamespace(in_transaction=False, execute=Mock(), commit=Mock(), rollback=Mock(),
        repair_status_projection_mismatches=Mock(return_value=report),
        get_by_id=Mock(side_effect=AssertionError('unscoped hydration forbidden')),
        get_by_exact_ref=Mock(return_value=returned))
    store._enqueue_record_exports = Mock(return_value=[])
    store._flush_committed_exports = Mock()
    export = Mock()
    monkeypatch.setattr('eimemory.storage.runtime_store.export_record_markdown', export)
    if mismatch:
        with pytest.raises(RuntimeError, match='hydration failed'):
            store.repair_status_projection_mismatches(scope=scope)
        export.assert_not_called()
        store.sqlite.commit.assert_not_called()
    else:
        result = store.repair_status_projection_mismatches(scope=scope)
        assert 'repaired_record_refs' not in result  # Public v1 shape stays stable.
        export.assert_called_once_with(tmp_path, returned)
    store.sqlite.get_by_exact_ref.assert_called_once_with(record.record_id, scope=scope, source_id='partition-a')
