"""Full-project model integration; uses an offline synthetic SQLite backup."""
import json
import sqlite3

import pytest
from deploy.rebuild_projection_snapshot import rebuild


def build_backup(tmp_path, *, mismatch=False):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    directory = tmp_path / 'backups'; directory.mkdir()
    path = directory / 'offline.sqlite'
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE records(record_id,kind,status,tenant_id,agent_id,workspace_id,user_id,'
                           'source_id,payload_json,payload_pointer_json,storage_key)')
        for index, user in enumerate(('alice', 'bob')):
            scope = ScopeRef(tenant_id='snapshot-test', agent_id='a', workspace_id='w', user_id=user)
            record = RecordEnvelope.create(kind='memory', title=f'Private {user}', scope=scope,
                                           content={'text': f'Only for {user}.'})
            record.record_id = 'same-projection-id'
            payload = record.to_dict()
            if mismatch and index == 1:
                payload['status'] = 'rejected'
            connection.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
                record.record_id, record.kind, record.status, scope.tenant_id, scope.agent_id,
                scope.workspace_id, scope.user_id, record.source_id, json.dumps(payload), '', str(index)))
        connection.commit()
    finally:
        connection.close()
    return path


def test_rebuild_creates_separate_scoped_v2_tree_without_index_switch(tmp_path):
    snapshot = build_backup(tmp_path)
    before = snapshot.read_bytes()
    output = tmp_path / 'new-tree'
    result = rebuild(snapshot, output)
    assert result['complete'] and result['exported_count'] == 2
    assert result['index_switch_performed'] is False
    files = list((output / 'qmd' / 'records').glob('v2-*/*.md'))
    assert len(files) == 2 and len({p.parent.name for p in files}) == 2
    assert all(len(p.parent.name) == 67 for p in files)
    assert snapshot.read_bytes() == before


def test_projection_payload_mismatch_does_not_publish_partial_tree(tmp_path):
    snapshot = build_backup(tmp_path, mismatch=True)
    output = tmp_path / 'new-tree'
    with pytest.raises(ValueError, match='identity_mismatch'):
        rebuild(snapshot, output)
    assert not output.exists()
