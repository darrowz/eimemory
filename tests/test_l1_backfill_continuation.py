"""Bounded exact-scope L1 backfill using synthetic sources only."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_pipeline import L1_EXTRACT_VERSION, backfill_l1_from_l0
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='synthetic', agent_id='audit', workspace_id='isolated', user_id='owner')

class EmptyLLM:
    def __init__(self):
        self.calls = 0
    def complete(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(text='[]')

@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    def blocked(*args, **kwargs):
        raise AssertionError('real provider prohibited')
    monkeypatch.setattr(CommandLLMClient, 'complete', blocked)

def row(store, record_id, *, meta=None, scope=SCOPE, episode=True, status='active', title='Synthetic turn', source='synthetic.turn'):
    text = 'User: Synthetic project Falcon belongs to Alice.\nAssistant: Acknowledged.'
    record = RecordEnvelope.create(kind='memory', title=title, summary=text,
        content={'text':text, 'memory_type':'conversation' if episode else 'fact'},
        scope=scope, source=source, source_id='synthetic', status=status,
        meta=meta if meta is not None else {'memory_type':'conversation' if episode else 'fact'})
    record.record_id=record_id
    return store.append(record)

def current(store, r):
    return store.get_by_exact_ref(r.record_id,scope=r.scope,source_id=r.source_id)

def test_repeated_default_calls_advance_beyond_completed_front_page(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        a=row(store,'mem_a')
        z=row(store,'mem_z')
        client=EmptyLLM()
        one=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,llm=client)
        two=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,llm=client)
        assert one['completed']==two['completed']==1
        assert not one['complete'] and one['next_cursor']
        assert two['complete'] and not two['next_cursor']
        assert client.calls==2
        assert all(business_metadata(current(store,r).meta)['l1_extraction_status']=='no_memory' for r in (a,z))


def test_large_non_l0_front_page_returns_explicit_continuation(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        for i in range(5): row(store,f'mem_{i}',episode=False)
        target=row(store,'mem_z')
        client=EmptyLLM()
        reports=[]
        cursor=''
        for _ in range(3):
            report=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,scan_limit=2,cursor=cursor,llm=client)
            reports.append(report)
            assert report['scanned_records']<=report['loaded_records']<=2
            cursor=report['next_cursor']
        assert reports[0]['scanned']==reports[1]['scanned']==0
        assert all(r['scan_exhausted'] and not r['complete'] and r['next_cursor'] for r in reports[:2])
        assert reports[2]['completed']==1 and reports[2]['complete'] and cursor==''
        assert client.calls==1
        assert business_metadata(current(store,target).meta)['l1_extraction_status']=='no_memory'


def test_cursor_does_not_skip_rows_when_updated_at_changes(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        targets=[row(store,'mem_'+i) for i in ('a','b','c')]
        client=EmptyLLM()
        first=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,llm=client)
        # Reorder updated_at between pages, as extraction and other mutations do.
        def reorder(sqlite):
            target=sqlite.get_by_exact_ref(targets[1].record_id,scope=SCOPE,source_id='synthetic')
            target.time.updated_at='2099-01-01T00:00:00Z'
            sqlite.upsert(target,commit=False)
            return None,[target],[]
        store.mutate_records_atomically(reorder)
        second=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,cursor=first['next_cursor'],llm=client)
        third=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,limit=1,cursor=second['next_cursor'],llm=client)
        assert first['completed']==second['completed']==third['completed']==1
        assert third['complete'] and client.calls==3
        assert all(business_metadata(current(store,r).meta).get('l1_extracted_at') for r in targets)


def test_selector_never_expands_write_scope_to_shared_or_other_users(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        own=row(store,'mem_own')
        shared=row(store,'mem_shared',scope=ScopeRef(**{**asdict(SCOPE),'user_id':''}))
        other=row(store,'mem_other',scope=ScopeRef(**{**asdict(SCOPE),'user_id':'other'}))
        report=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,llm=EmptyLLM())
        assert report['completed']==report['scanned_records']==1 and report['complete']
        assert business_metadata(current(store,own).meta).get('l1_extracted_at')
        for r in (shared,other): assert not business_metadata(current(store,r).meta).get('l1_extracted_at')

@pytest.mark.parametrize('projection', ['missing','wrong_lane'])
def test_legacy_l0_is_found_without_trusting_recall_projection(tmp_path,projection):
    with closing(RuntimeStore(tmp_path)) as store:
        target=row(store,'mem_legacy') # No memory_layer/capture_origin/completed-turn title.
        with store._lock:
            if projection=='missing': store.sqlite.execute('DELETE FROM recall_index')
            else: store.sqlite.execute("UPDATE recall_index SET lane='primary'")
            store.sqlite.commit()
        report=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,llm=EmptyLLM())
        assert report['completed']==1 and business_metadata(current(store,target).meta).get('l1_extracted_at')

@pytest.mark.parametrize('retry', [False,True])
def test_completion_and_legacy_markers_keep_python_business_metadata_semantics(tmp_path,retry):
    with closing(RuntimeStore(tmp_path)) as store:
        base={'memory_type':'conversation'}
        authoritative=row(store,'mem_a',meta={**base,'business_meta':{
            'l1_extract_version':L1_EXTRACT_VERSION,'l1_extraction_status':'no_memory'}})
        legacy=row(store,'mem_b',meta={**base,'business_meta':{'l1_extracted_at':'legacy'}})
        whitespace=row(store,'mem_c',meta={**base,'l1_extracted_at':'\u2003\n'})
        # Top-level presence, including empty values, must override a stale mirror.
        pending=row(store,'mem_d',meta={**base,'l1_extraction_status':'',
            'business_meta':{'l1_extract_version':L1_EXTRACT_VERSION,'l1_extraction_status':'stored'}})
        report=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,llm=EmptyLLM(),retry_legacy=retry)
        assert report['completed']==(3 if retry else 2)
        assert report['legacy_retried']==int(retry)
        assert report['skipped']==(1 if retry else 2)
        assert not business_metadata(current(store,authoritative).meta).get('l1_extracted_at')
        marker=business_metadata(current(store,legacy).meta)['l1_extracted_at']
        assert (marker!='legacy') if retry else (marker=='legacy')
        assert report['complete']


def test_selector_is_bounded_and_lock_guarded(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        for i in range(4): row(store,f'mem_{i}',episode=False)
        original=store.sqlite._record_from_storage_row
        hydrated=[]
        def count(record_row,**kwargs):
            hydrated.append(record_row['storage_key'])
            return original(record_row,**kwargs)
        monkeypatch.setattr(store.sqlite,'_record_from_storage_row',count)
        page,more=store.list_l1_backfill_page(scope=SCOPE,limit=2)
        assert len(page)==len(hydrated)==2 and more
        with pytest.raises(RuntimeError,match='sqlite_connection_used_without_runtime_lock'):
            store.sqlite.list_l1_backfill_page(scope=SCOPE,limit=2)


def test_invalid_and_cross_scope_cursors_fail_closed(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        for i in range(2): row(store,f'mem_{i}',episode=False)
        api=MemoryAPI(store)
        first=backfill_l1_from_l0(api,scope=SCOPE,scan_limit=1,llm=EmptyLLM())
        for cursor in ('not-base64', 'e30=', first['next_cursor']):
            scope=SCOPE if cursor!=first['next_cursor'] else ScopeRef(**{**asdict(SCOPE),'user_id':'other'})
            with pytest.raises(ValueError,match='l1_backfill_cursor_invalid'):
                backfill_l1_from_l0(api,scope=scope,cursor=cursor,llm=EmptyLLM())
        with pytest.raises(ValueError,match='l1_backfill_cursor_invalid'):
            backfill_l1_from_l0(api,scope=SCOPE,cursor=first['next_cursor'],retry_legacy=True,llm=EmptyLLM())


def test_empty_page_has_no_false_continuation(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        row(store,'mem_inactive',status='superseded')
        report=backfill_l1_from_l0(MemoryAPI(store),scope=SCOPE,llm=EmptyLLM(),scan_limit=99999)
        assert report['complete'] and not report['scan_exhausted'] and report['next_cursor']==''
        assert report['scanned_records']==0 and report['scan_limit']==1000


def test_service_passes_cursor_and_scan_budget(monkeypatch):
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    seen={}
    def backfill(api,**kwargs):
        seen.update(kwargs)
        return {'ok':True,'next_cursor':'continue'}
    monkeypatch.setattr('eimemory.knowledge.l1_pipeline.backfill_l1_from_l0',backfill)
    service=AgentRuntimeMemoryService(SimpleNamespace(memory='fake'))
    result=service.backfill_l1(channel='hermes',scope=asdict(SCOPE),cursor='supplied',scan_limit=17)
    assert seen['cursor']=='supplied' and seen['scan_limit']==17 and result['next_cursor']=='continue'


def test_cli_repair_passes_continuation_env_and_prints_report(monkeypatch,capsys):
    from eimemory.cli import l1_worker
    seen={}
    monkeypatch.setenv('EIMEMORY_ROOT','synthetic-never-opened')
    monkeypatch.setenv('EIMEMORY_L1_WORKER_ACTION','repair')
    monkeypatch.setenv('EIMEMORY_L1_REPAIR_CURSOR','supplied')
    monkeypatch.setenv('EIMEMORY_L1_REPAIR_SCAN_LIMIT','17')
    monkeypatch.setattr(l1_worker.Runtime,'create',lambda **kwargs:SimpleNamespace(close=lambda:None))
    class Service:
        def __init__(self,runtime): pass
        def backfill_l1(self,**kwargs):
            seen.update(kwargs)
            return {'ok':True,'complete':False,'scan_exhausted':True,'next_cursor':'continue'}
    monkeypatch.setattr(l1_worker,'AgentRuntimeMemoryService',Service)
    assert l1_worker.main()==0
    report=json.loads(capsys.readouterr().out)
    assert seen['cursor']=='supplied' and seen['scan_limit']==17
    assert not report['complete'] and report['next_cursor']=='continue'
