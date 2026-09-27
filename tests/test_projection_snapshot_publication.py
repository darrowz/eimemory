"""Snapshot transport/publication tests; full-model export is tested separately."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest
from deploy import rebuild_projection_snapshot as projection


def test_snapshot_revalidation_failure_never_publishes(tmp_path, monkeypatch):
    backup = tmp_path / 'backup'; backup.mkdir()
    snapshot = backup / 'db.sqlite'; snapshot.write_bytes(b'placeholder')
    output = tmp_path / 'new-projection'

    @contextmanager
    def changed(*_args, **_kwargs):
        yield object()
        raise ValueError('snapshot_changed_during_review')

    monkeypatch.setattr(projection, 'open_snapshot', changed)
    monkeypatch.setattr(projection, '_build_into', lambda *_args, **_kwargs: {'complete': True})
    with pytest.raises(ValueError, match='snapshot_changed'):
        projection.rebuild(snapshot, output)
    assert not output.exists()
    assert not list(tmp_path.glob('.eimemory-projection-*'))


def test_existing_projection_is_never_replaced(tmp_path):
    snapshot = tmp_path / 'backup' / 'db.sqlite'
    output = tmp_path / 'existing'; output.mkdir()
    (output / 'evidence.md').write_text('retained')
    with pytest.raises(FileExistsError):
        projection.rebuild(snapshot, output)
    assert (output / 'evidence.md').read_text() == 'retained'


@pytest.mark.parametrize('count,cold,limit,reason', [(2,False,1,'limit'), (1,True,10,'cold_export')])
def test_bounded_inline_contract_checked_before_model_import(tmp_path, count, cold, limit, reason):
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TABLE records(record_id,kind,status,tenant_id,agent_id,workspace_id,user_id,'
                           'source_id,payload_json,payload_pointer_json,storage_key)')
        for i in range(count):
            connection.execute("INSERT INTO records VALUES(?,'memory','active','t','a','w','u','s','{}',?,?)",
                               (f'r{i}', '{}' if cold else '', f'k{i}'))
        with pytest.raises(ValueError, match=reason):
            projection._build_into(connection, tmp_path, max_records=limit)
    finally:
        connection.close()
