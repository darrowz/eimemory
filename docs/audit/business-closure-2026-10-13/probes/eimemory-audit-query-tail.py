import sys,json
sys.path.insert(0,'/workspace/eimemory')
from contextlib import closing
from tempfile import TemporaryDirectory
from eimemory.models.records import RecordEnvelope,ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore
s=ScopeRef(tenant_id='audit',agent_id='audit',workspace_id='audit',user_id='audit')
prefix='Please retrieve previous stored project account configuration details regarding documented historical ownership'
def put(st,rid,title,body,newer):
 r=RecordEnvelope.create(kind='memory',title=title,summary=body,content={'text':body,'memory_type':'fact'},scope=s,source_id='hermes',source='synthetic.l1',meta={'memory_type':'fact','force_capture':True})
 r.record_id=rid
 r.time.updated_at=r.time.created_at=r.time.occurred_at='2026-02-01T00:00:00Z' if newer else '2026-01-01T00:00:00Z'
 st.append(r)
with TemporaryDirectory(prefix='eimemory-audit-tail-') as tmp,closing(RuntimeStore(tmp)) as st:
 put(st,'mem_target','Falconquasar database owner','Falconquasar database owner is owneralpha',False)
 for i in range(45):put(st,f'mem_distractor_{i}','Historical project reference '+str(i),prefix,True)
 src=SQLiteCandidateSource(st)
 out=[]
 for query in ('Falconquasar',prefix+' Falconquasar'):
  req=CandidateRequest.create(query=query,scope=s,kinds=['memory'],source_ids=['hermes'],limit=14,budget=48)
  batch=src.search(req)
  with st._lock:
   rows,report=st.sqlite._candidate_rows(query=query,kinds=['memory'],scope=s,limit=14,recall_filters={'_exact_scope':True,'_source_ids':['hermes'],'candidate_limit':48})
   fts=st.sqlite._fts_query(query)
  out.append({'query_chars':len(query),'tail_in_fts_query':'falconquasar' in fts,'target_in_candidate':any(r['record_id']=='mem_target' for r in rows),'target_returned':any(h.ref.record_id=='mem_target' for h in batch.hits),'candidate_count':len(rows),'return_count':len(batch.hits),'fallback':batch.diagnostic_dict().get('fallback')})
 print(json.dumps(out))
