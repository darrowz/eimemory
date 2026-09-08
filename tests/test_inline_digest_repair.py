import json
from dataclasses import asdict

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.inline_digest_repair import repair_legacy_l1_inline_digests
from eimemory.storage.inline_digest_repair import repair_inline_projection_timestamps


def test_timestamp_repair_preserves_verified_envelope(tmp_path):
    store=RuntimeStore(tmp_path)
    scope=ScopeRef(agent_id='main',workspace_id='project',user_id='owner')
    record=RecordEnvelope.create(kind='memory',title='Original',scope=scope)
    try:
        store.append(record)
        before=store.sqlite.conn.execute('SELECT payload_json,payload_digest FROM records WHERE record_id=?',(record.record_id,)).fetchone()
        store.sqlite.conn.execute('UPDATE records SET updated_at=? WHERE record_id=?',('2000-01-01T00:00:00Z',record.record_id))
        store.sqlite.conn.commit()
        assert repair_inline_projection_timestamps(store,scope=scope)['eligible']==1
        assert repair_inline_projection_timestamps(store,scope=scope,apply=True)['repaired']==1
        after=store.sqlite.conn.execute('SELECT payload_json,payload_digest,updated_at FROM records WHERE record_id=?',(record.record_id,)).fetchone()
        assert tuple(after[:2])==tuple(before)
        assert after['updated_at']==record.time.updated_at
        assert repair_inline_projection_timestamps(store,scope=scope,apply=True)['repaired']==0
        store.sqlite.conn.execute('UPDATE records SET payload_digest=?,updated_at=? WHERE record_id=?',('invalid','2000-01-01T00:00:00Z',record.record_id))
        store.sqlite.conn.commit()
        refused=repair_inline_projection_timestamps(store,scope=scope,apply=True)
        assert refused['repaired']==0 and refused['unproven']==[record.record_id]
    finally:store.close()


@pytest.mark.parametrize('tamper', [False, True])
def test_digest_repair_requires_complete_original_preimage(tmp_path, tamper):
    store=RuntimeStore(tmp_path)
    base=ScopeRef(agent_id='main',workspace_id='project',user_id='owner')
    exact=ScopeRef(**{**asdict(base),'workspace_id':'project::channel::hermes'})
    record=RecordEnvelope.create(kind='memory',title='Original',summary='Verified body',scope=exact,source_id='hermes')
    try:
        store.append(record)
        row=store.sqlite.conn.execute('SELECT payload_json FROM records WHERE record_id=?',(record.record_id,)).fetchone()
        payload=json.loads(row['payload_json'])
        payload['meta'].update(l1_extracted_at='1',l1_backfill_batch='legacy-batch')
        if tamper:payload['summary']='Altered body'
        raw=json.dumps(payload)
        store.sqlite.conn.execute('UPDATE records SET payload_json=? WHERE record_id=?',(raw,record.record_id))
        store.sqlite.conn.commit()
        assert store.get_by_exact_ref(record.record_id,scope=exact,source_id='hermes') is None
        dry=repair_legacy_l1_inline_digests(store,scope=base)
        assert dry['eligible']==(0 if tamper else 1) and dry['repaired']==0
        assert store.get_by_exact_ref(record.record_id,scope=exact,source_id='hermes') is None
        fixed=repair_legacy_l1_inline_digests(store,scope=base,apply=True)
        assert fixed['repaired']==(0 if tamper else 1)
        restored=store.get_by_exact_ref(record.record_id,scope=exact,source_id='hermes')
        assert (restored is None)==tamper
        assert store.sqlite.conn.execute('SELECT payload_json FROM records WHERE record_id=?',(record.record_id,)).fetchone()[0]==raw
        assert repair_legacy_l1_inline_digests(store,scope=base,apply=True)['repaired']==0
    finally:store.close()


def test_digest_repair_rejects_incomplete_owner(tmp_path):
    store=RuntimeStore(tmp_path)
    try:
        with pytest.raises(ValueError,match='exact_owner'):
            repair_legacy_l1_inline_digests(store,scope=ScopeRef())
    finally:store.close()
