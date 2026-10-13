import sys,json,tempfile
sys.path.insert(0,'/workspace/eimemory')
sys.path.insert(0,'/workspace/eimemory/tests')
from contextlib import closing
from pathlib import Path
from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope,ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.retrieval.lightweight_admission import LightweightAdmission,LightweightConfig
from eimemory.storage import sqlite_store,runtime_store
import pytest
import test_recall_budget_reserve as budget_tests
out=[]
for mode in ('default','lightweight'):
 with tempfile.TemporaryDirectory(prefix='eimemory-audit-revoke-') as d,closing(RuntimeStore(d)) as store:
  scope=ScopeRef(tenant_id='audit',agent_id='audit',workspace_id='audit',user_id='audit')
  record=store.append(RecordEnvelope.create(kind='memory',title='Synthetic account allocation',summary='Synthetic account allocation approved for owner.',content={'text':'Synthetic account allocation approved for owner.','memory_type':'fact'},meta={'memory_type':'fact','force_capture':True},scope=scope,source_id='hermes'))
  memory=MemoryAPI(store)
  engine=memory.recall_engine
  if mode=='lightweight':engine.relevance_admission=LightweightAdmission(LightweightConfig(enabled=True))
  reads=[0]
  original=engine._hydrate_records_batch
  def hydrate(rows,*,deadline_at):
   reads[0]+=1
   if reads[0]==2:
    current=store.get_by_exact_ref(record.record_id,scope=scope,source_id='hermes')
    current.status='inactive'
    store.rewrite(current)
   return original(rows,deadline_at=deadline_at)
  engine._hydrate_records_batch=hydrate
  items,report=engine._select_post_fusion_items([record],query=record.title,limit=1,fusion_state={},component_hints_by_ref={},validate=lambda item:True)
  out.append({'mode':mode,'fresh_authority_reads':reads[0],'canonical_status':store.get_by_exact_ref(record.record_id,scope=scope,source_id='hermes').status,'returned_count':len(items),'status':report.get('status'),'drops':report.get('dropped_reasons')})
with tempfile.TemporaryDirectory(prefix='eimemory-audit-clock-') as d,pytest.MonkeyPatch.context() as mp:
 mp.setattr(runtime_store,'perf_counter',lambda:sqlite_store.perf_counter())
 try:
  budget_tests.test_sqlite_stops_scoring_at_collection_deadline(Path(d),mp)
  out.append({'legacy_sqlite_deadline_same_test_with_runtime_clock_bound':'passed'})
 except AssertionError as exc:out.append({'legacy_sqlite_deadline_same_test_with_runtime_clock_bound':str(exc) or 'failed'})
print(json.dumps(out))
