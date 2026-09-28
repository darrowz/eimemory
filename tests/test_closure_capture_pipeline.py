"""Filesystem and SQLite integration using a deliberately small store adapter."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import pytest

from eimemory.models.records import RecordEnvelope,ScopeRef
from eimemory.ops.closure_capture import process_capture,read_source
from eimemory.ops.release_closure_failure import record_release_closure_failure
from test_closure_pipeline_contract import blocked

SCOPE={'tenant_id':'default','agent_id':'a','workspace_id':'w','user_id':'u'}

class SQLiteFixtureStore:
    """Real SQLite uniqueness/transactions; not the project's complete RuntimeStore."""
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS records (id TEXT, scope TEXT, payload TEXT, PRIMARY KEY(id,scope))')
    def append(self,record,*,existing_match=None):
        with sqlite3.connect(self.path,timeout=10) as c:
            c.execute('BEGIN IMMEDIATE')
            key=json.dumps(asdict(record.scope),sort_keys=True)
            row=c.execute('SELECT payload FROM records WHERE id=? AND scope=?',(record.record_id,key)).fetchone()
            if row:
                saved=RecordEnvelope.from_dict(json.loads(row[0]))
                if existing_match and not existing_match(saved):raise ValueError('conflict')
                return saved
            c.execute('INSERT INTO records VALUES (?,?,?)',(record.record_id,key,json.dumps(record.to_dict())))
        return record

class FixtureRuntime:
    def __init__(self,path):self.store=SQLiteFixtureStore(path)
    def close(self):pass


def test_same_snapshot_is_archived_classified_and_persisted(tmp_path):
    source=tmp_path/'raw.json';raw=json.dumps(blocked('storage_failed')).encode()
    source.write_bytes(raw)
    result=process_capture(source,evidence_dir=tmp_path/'captures',scope=SCOPE,
                           expected_commit='a'*40,attempt_id='attempt-1',closure_exit_status=1,
                           runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert result['status']=='failure_detected' and result['recording_ok']
    assert result['incident_record_id'] and result['repair_complete'] is False
    capture=Path(result['capture']['path'])
    assert (capture/'source.bin').read_bytes()==raw
    assert result['capture']['source_sha256']==sha256(raw).hexdigest()
    saved=json.loads((capture/'summary.json').read_text())
    assert saved['report_digest']==result['report_digest']
    with sqlite3.connect(tmp_path/'db.sqlite') as c:
        record=json.loads(c.execute('SELECT payload FROM records').fetchone()[0])
    assert record['content']['detector_report']['report_digest']==result['report_digest']


def test_no_reread_when_original_is_replaced_after_load(tmp_path,monkeypatch):
    import eimemory.ops.closure_capture as module
    source=tmp_path/'source.json';raw=json.dumps(blocked('storage_failed')).encode();source.write_bytes(raw)
    read=module.read_source
    def replace_after_read(path):
        original=read(path);source.write_text(json.dumps(blocked('production_dataset_not_ready')));return original
    monkeypatch.setattr(module,'read_source',replace_after_read)
    out=process_capture(source,evidence_dir=tmp_path/'caps',scope=SCOPE,
                        runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert out['status']=='failure_detected'
    assert Path(out['capture']['path'],'source.bin').read_bytes()==raw


@pytest.mark.parametrize('raw',[b'{',b'{"ok":true,"ok":false}',b'{"x":NaN}',b'[]'])
def test_malformed_output_has_incident_and_preserves_bytes(tmp_path,raw):
    p=tmp_path/'raw';p.write_bytes(raw)
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,expected_commit='a'*40,
                        runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert out['incident_record_id'] and out['status']=='failure_detected'
    assert Path(out['capture']['path'],'source.bin').read_bytes()==raw


def test_recording_failure_preserves_snapshot_and_nonzero(tmp_path):
    p=tmp_path/'raw';p.write_text(json.dumps(blocked('storage_failed')))
    def unavailable():raise RuntimeError('secret-database-connection-string')
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,runtime_factory=unavailable)
    assert not out['recording_ok'] and out['exit_code']==2 and out['capture_saved']
    assert out['status']=='incident_recording_failed' and not out['incident_record_id']
    assert 'secret-database-connection-string' not in json.dumps(out)


def test_pure_wait_does_not_construct_runtime(tmp_path):
    p=tmp_path/'raw';p.write_text(json.dumps(blocked('production_dataset_not_ready')))
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,
                        runtime_factory=lambda:pytest.fail('wait must not manufacture incident'))
    assert out['status']=='evidence_waiting' and not out['incident_record_id']


def test_unknown_aggregate_is_durable_diagnosis(tmp_path):
    p=tmp_path/'raw';p.write_text(json.dumps(blocked()))
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,
                        runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert out['status']=='diagnosis_required' and out['incident_record_id']
    assert not out['repair_complete']


def test_concurrent_duplicate_registration_and_scope_isolation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    runtime=FixtureRuntime(tmp_path/'db.sqlite')
    def record(_):return record_release_closure_failure(runtime,scope=SCOPE,closure_report=blocked('storage_failed'),detected_at='now')['incident_record_id']
    with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(record,range(8)))
    assert len(set(ids))==1
    other=record_release_closure_failure(runtime,scope={**SCOPE,'user_id':'other'},closure_report=blocked('storage_failed'),detected_at='now')
    assert other['incident_record_id']!=ids[0]
    with sqlite3.connect(tmp_path/'db.sqlite') as c:assert c.execute('SELECT count(*) FROM records').fetchone()[0]==2


@pytest.mark.skipif(os.name!='posix',reason='POSIX FIFO contract')
def test_fifo_is_rejected_without_open_blocking(tmp_path):
    p=tmp_path/'fifo';os.mkfifo(p)
    with pytest.raises(ValueError):read_source(p)


@pytest.mark.skipif(os.name!='posix',reason='POSIX permission contract')
def test_capture_permissions_are_private(tmp_path):
    p=tmp_path/'raw';p.write_text(json.dumps(blocked('production_dataset_not_ready')))
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE)
    d=Path(out['capture']['path']);assert d.stat().st_mode&0o777==0o700
    for f in d.iterdir():assert f.stat().st_mode&0o777==0o600


def test_missing_input_is_not_claimed_to_be_archived(tmp_path):
    out=process_capture(tmp_path/'missing',evidence_dir=tmp_path/'caps',scope=SCOPE,
                        expected_commit='a'*40,runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert not out['capture_saved'] and out['incident_record_id']
    assert not Path(out['capture']['path'],'source.bin').exists()


def test_close_error_does_not_erase_saved_input(tmp_path):
    class BadClose(FixtureRuntime):
        def close(self):raise RuntimeError('connection-close-detail')
    p=tmp_path/'raw';p.write_text(json.dumps(blocked('storage_failed')))
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,
                        runtime_factory=lambda:BadClose(tmp_path/'db.sqlite'))
    assert out['capture_saved'] and out['exit_code']==2
    assert out['status']=='runtime_close_failed'
    assert out['incident_record_id']


def test_scope_mismatch_is_a_first_class_report_failure(tmp_path):
    p=tmp_path/'raw';report=blocked('production_dataset_not_ready')
    report['scope']={**SCOPE,'user_id':'foreign'};p.write_text(json.dumps(report))
    out=process_capture(p,evidence_dir=tmp_path/'caps',scope=SCOPE,
                        runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert out['status']=='failure_detected'
    assert any(s['code']=='closure_report_scope_mismatch' for s in out['failure_signals']['hard_errors'])


def test_distinct_malformed_bytes_are_not_one_incident(tmp_path):
    source=tmp_path/'raw'
    source.write_bytes(b'{broken-one')
    one=process_capture(source,evidence_dir=tmp_path/'caps',scope=SCOPE,expected_commit='a'*40,
                        attempt_id='same',runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    source.write_bytes(b'{broken-two')
    two=process_capture(source,evidence_dir=tmp_path/'caps',scope=SCOPE,expected_commit='a'*40,
                        attempt_id='same',runtime_factory=lambda:FixtureRuntime(tmp_path/'db.sqlite'))
    assert one['incident_record_id']!=two['incident_record_id']
    assert one['capture']['source_sha256']!=two['capture']['source_sha256']
