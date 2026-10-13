import sys, json
from dataclasses import asdict
sys.path.insert(0, '/workspace/eimemory')
from contextlib import closing
from tempfile import TemporaryDirectory
from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.authority_gate import authoritative_identity_exists
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.metadata import business_metadata
s=ScopeRef(tenant_id='audit',agent_id='audit',workspace_id='audit',user_id='audit')
with TemporaryDirectory(prefix='eimemory-identity-audit-') as tmp, closing(RuntimeStore(tmp)) as store:
    for i in range(80):
        r=RecordEnvelope.create(kind='memory',title='Audited unique account marker',summary='Audited unique account marker',content={'text':'Audited unique account marker','memory_type':'fact'},scope=s,source_id='hermes',source='synthetic.l1',meta={'memory_type':'fact','force_capture':True,**({'valid_until':'2000-01-01T00:00:00Z'} if 0<i<79 else {})})
        r.record_id=f'mem_audit_{i:02d}'
        store.append(r)
    memory=MemoryAPI(store)
    engine=memory.recall_engine
    req=CandidateRequest.create(query='Audited unique account marker',scope=s,kinds=['memory'],source_ids=['hermes'],target_source_id='hermes')
    rows=store.search_identity_candidates(query=req.query,kinds=['memory'],scope=s,limit=100,source_ids=['hermes'])
    valid=[row['record_id'] for row in rows if not memory._is_temporally_stale_memory(store.get_by_exact_ref(row['record_id'],scope=s,source_id='hermes'))]
    bundle=memory.recall(query=req.query,scope=asdict(s),limit=1,task_context={'source_ids':['hermes'],'target_source_id':'hermes','scope_strategy':'exact'})
    from eimemory.adapters.create_safety_gate import gate_host_create
    gate=gate_host_create(None,text='synthetic new memory',scope=asdict(s),force=True,fusion_hint=bundle.explanation)
    print(json.dumps({'actual_valid_identity_count':len(valid),'all_indexed':len(rows),'authority_result':authoritative_identity_exists(engine,query=req.query,request=req,target_source_id='hermes'),'fusion':bundle.explanation.get('fusion'),'items':[i.record_id for i in bundle.items],'gate_allow_with_explicit_force':gate.get('allow'),'gate_reason':gate.get('reason')}))
