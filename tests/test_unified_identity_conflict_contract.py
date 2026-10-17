"""Derived IDs are not payload versions or permission to replace an occupied owner."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from eimemory import identity
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge.extract import extract_paper_memory
from eimemory.models.entity_records import EntityRecord
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.jsonl import payload_digest

LEGACY=ScopeRef('synthetic','legacy-owner','embodied','owner')
CURRENT=ScopeRef('synthetic','current-owner','embodied','owner')

@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setattr(identity,'HONGTU_AGENT_ID','current-owner')
    monkeypatch.setattr(identity,'legacy_agent_ids',lambda:('legacy-owner',))
    result=RuntimeStore(tmp_path)
    yield result
    result.close()

def rows(store):
    with store._lock:
        return [dict(row) for row in store.sqlite.execute('SELECT * FROM records ORDER BY storage_key')]

def entity(scope):
    return EntityRecord('ent_fixed','psrc_fixed','Orchid').to_record(scope=scope)

def test_repeat_derived_generation_retains_ids_but_changes_provenance_and_clock(monkeypatch):
    modules=('paper_sources','paper_extracts','claim_cards','entity_records','relation_records','knowledge_pages')
    def generate(scope,stamp,authority):
        for name in modules:monkeypatch.setattr('eimemory.models.'+name+'.now_iso',lambda:stamp)
        extraction=extract_paper_memory(paper_source_id='psrc_fixed',title='Orchid Memory',
            abstract='Orchid Memory improves retrieval. Orchid Memory requires evidence.',
            provenance={'authority':authority})
        records=extraction.to_records(scope=scope)
        records+=compile_paper_knowledge(extraction=extraction).to_records(scope=scope)
        records.append(PaperSource('psrc_fixed','metadata',title='Orchid Memory',
                                  provenance={'authority':authority}).to_record(scope=scope))
        return {item.record_id:item for item in records}
    old=generate(LEGACY,'2026-01-01T00:00:00Z','old')
    new=generate(CURRENT,'2026-01-02T00:00:00Z','new')
    assert old.keys()==new.keys()
    assert {item.kind for item in old.values()}=={'paper_source','paper_extract','claim_card','entity_record','relation_record','knowledge_page'}
    for rid,before in old.items():
        after=new[rid]
        assert before.source==after.source and before.source_id==after.source_id
        assert before.time.created_at!=after.time.created_at
        assert before.time.occurred_at!=after.time.occurred_at
        assert before.provenance!=after.provenance
        assert before.content!=after.content

@pytest.mark.parametrize('difference',['content','provenance','created_at','occurred_at','source_id','equivalent'])
def test_occupied_target_preserves_both_rows_and_failed_outcome(store,difference):
    source=store.append(entity(LEGACY))
    # Start with exactly the proposed payload, then vary one independent authority field.
    target=identity.normalize_hongtu_record(source)
    if difference=='content':target.content['salience']=0.99
    elif difference=='provenance':target.provenance['authority']='independent-writer'
    elif difference=='created_at':target.time=replace(target.time,created_at='2026-01-02T00:00:00Z')
    elif difference=='occurred_at':target.time=replace(target.time,occurred_at='2026-01-02T00:00:00Z')
    elif difference=='source_id':target.source_id='hermes'
    store.append(target);before=rows(store)
    result=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert result['ok'] is False and result['repaired_count']==0
    assert result['outcome_counts']=={'target_conflict':1}
    assert rows(store)==before

def test_unoccupied_target_migrates_without_changing_business_evidence(store):
    source=store.append(entity(LEGACY));original=source.to_dict()
    result=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert result['ok'] is True and result['repaired_count']==1
    target=store.get_by_exact_ref(source.record_id,scope=CURRENT,source_id=source.source_id)
    assert target.content==original['content'] and target.provenance==original['provenance']
    assert target.time.created_at==original['time']['created_at']
    assert target.time.occurred_at==original['time']['occurred_at']
    assert store.get_by_exact_ref(source.record_id,scope=LEGACY,source_id=source.source_id) is None

def test_concurrent_target_writer_is_preserved_and_migration_stays_failed(store,tmp_path,monkeypatch):
    source=store.append(entity(LEGACY));snapshot=store.read_consistent
    writer=RuntimeStore(tmp_path)
    try:
        target=entity(CURRENT);target.content['authority']='concurrent-writer'
        def after_discovery(callback):
            result=snapshot(callback)
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(writer.append,target).result(timeout=10)
            return result
        monkeypatch.setattr(store,'read_consistent',after_discovery)
        result=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
        assert result['ok'] is False and result['outcome_counts']=={'target_conflict':1}
        assert store.get_by_exact_ref(source.record_id,scope=LEGACY,source_id=source.source_id).to_dict()==source.to_dict()
        assert store.get_by_exact_ref(target.record_id,scope=CURRENT,source_id=target.source_id).to_dict()==target.to_dict()
    finally:writer.close()

def test_corrupt_source_is_not_reclassified_as_benign_collision(store):
    source=store.append(entity(LEGACY));store.append(entity(CURRENT))
    with store._lock:
        store.sqlite.execute('UPDATE records SET payload_digest=? WHERE agent_id=?',('0'*64,LEGACY.agent_id))
        store.sqlite.commit()
    before=rows(store)
    result=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert result['ok'] is False
    assert result['outcome_counts']=={'source_projection_or_digest_mismatch':1}
    assert rows(store)==before

@pytest.mark.parametrize('damage',['json','checksum','scope','archive'])
def test_unavailable_physical_row_remains_a_blocker(store,damage):
    source=store.append(entity(LEGACY))
    with store._lock:
        if damage=='json':store.sqlite.execute('UPDATE records SET payload_json=?',('{',))
        elif damage=='checksum':store.sqlite.execute('UPDATE records SET payload_digest=?',('0'*64,))
        elif damage=='archive':store.sqlite.execute('UPDATE records SET payload_pointer_json=?',('{}',))
        else:
            raw=source.to_dict();raw['scope']['tenant_id']='foreign'
            store.sqlite.execute('UPDATE records SET payload_json=?,payload_digest=?',(json.dumps(raw),payload_digest(raw)))
        store.sqlite.commit()
    before=rows(store)
    report=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert report['ok'] is False and report['repaired_count']==0
    assert report['blocked_count']==1
    assert rows(store)==before

@pytest.mark.parametrize('limit',[None,1])
def test_raw_page_cursor_does_not_stop_at_a_fully_unhydratable_page(store,limit):
    with store._lock:
        for index in range(501):
            record=entity(LEGACY);record.record_id=f'ent_page_{index:04d}'
            store.sqlite.upsert(record,commit=False)
        store.sqlite.execute('UPDATE records SET payload_digest=? WHERE record_id<>?',('0'*64,'ent_page_0000'))
        store.sqlite.commit()
    report=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True,limit=limit)
    assert report['ok'] is False
    assert report['blocked_count']==(500 if limit is None else 1)
    assert report['repaired_count']==(1 if limit is None else 0)
    assert report['repair_outcomes_truncated'] is (limit is None)

def test_foreign_corruption_does_not_expand_repair_authority(store):
    store.append(entity(LEGACY))
    foreign=store.append(entity(replace(LEGACY,tenant_id='foreign')))
    with store._lock:
        store.sqlite.execute('UPDATE records SET payload_digest=? WHERE tenant_id=?',('0'*64,'foreign'))
        store.sqlite.commit()
    before=[row for row in rows(store) if row['tenant_id']=='foreign']
    report=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert report['ok'] is True and report['repaired_count']==1
    assert [row for row in rows(store) if row['tenant_id']=='foreign']==before
