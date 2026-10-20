import gc
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.compatibility import migration_helpers as helper
from eimemory.models.records import RecordEnvelope,ScopeRef


class TrackedEnvelope:
    def __init__(self,record):self.record=record
    def __getattr__(self,name):return getattr(self.record,name)


def test_authority_export_keeps_only_a_page_of_hydrated_records(tmp_path,monkeypatch):
    refs=[];count=650
    def page(*,limit,offset):
        gc.collect()
        assert sum(ref() is not None for ref in refs)<=2
        result=[]
        for number in range(offset,min(offset+limit,count)):
            record=TrackedEnvelope(RecordEnvelope.create(kind='memory',scope=ScopeRef(),title=str(number),content={'text':'x'*8192}))
            refs.append(weakref.ref(record));result.append(record)
        return result
    runtime=SimpleNamespace(store=SimpleNamespace(list_records=page,log=SimpleNamespace(path=None)))
    report=helper.backup_create(runtime,tmp_path/'backup')
    assert report['record_count']==count and helper.backup_verify(tmp_path/'backup')['ok']
    assert not list(tmp_path.glob('.eimemory-export-*'))


def test_log_recovery_keeps_offsets_instead_of_all_envelopes(tmp_path,monkeypatch):
    path=tmp_path/'records.jsonl'
    with path.open('w',encoding='utf-8') as output:
        for number in range(80):
            record=RecordEnvelope.create(kind='memory',scope=ScopeRef(),title=str(number),content={'text':'x'*8192})
            output.write(json.dumps(record.to_dict())+'\n')
    del record
    original=RecordEnvelope.from_dict;refs=[]
    def hydrate(payload):
        assert sum(ref() is not None for ref in refs)<=4
        value=TrackedEnvelope(original(payload));refs.append(weakref.ref(value));return value
    monkeypatch.setattr(RecordEnvelope,'from_dict',staticmethod(hydrate))
    runtime=SimpleNamespace(store=SimpleNamespace(list_records=lambda **_:[],log=SimpleNamespace(path=path)))
    report=helper.backup_create(runtime,tmp_path/'backup')
    assert report['record_count']==80


def test_recovery_log_change_between_passes_never_publishes_a_backup(tmp_path,monkeypatch):
    record=RecordEnvelope.create(kind='memory',scope=ScopeRef(),title='original')
    path=tmp_path/'records.jsonl';path.write_text(json.dumps(record.to_dict())+'\n')
    original=helper.JsonlLog.scan_strict;calls=[]
    def scan(log):
        calls.append(True)
        if len(calls)==2:
            changed=record.to_dict();changed['title']='changed'
            path.write_text(json.dumps(changed)+'\n')
        return original(log)
    monkeypatch.setattr(helper.JsonlLog,'scan_strict',scan)
    runtime=SimpleNamespace(store=SimpleNamespace(list_records=lambda **_:[],log=SimpleNamespace(path=path)))
    with pytest.raises(ValueError,match='journal_changed'):helper.backup_create(runtime,tmp_path/'backup')
    assert not (tmp_path/'backup.manifest.json').exists()
    assert not list(tmp_path.glob('.eimemory-export-*'))
