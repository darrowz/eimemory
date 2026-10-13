import sys,tempfile,json
from pathlib import Path
from dataclasses import asdict
sys.path.insert(0,'/workspace/eimemory')
from eimemory.api.runtime import Runtime
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.models.records import ScopeRef, RecordEnvelope
class ObservedSQLiteSource(SQLiteCandidateSource):
    def __init__(self,store):
        super().__init__(store)
        self.calls=0
    def search(self,request):
        self.calls+=1
        return super().search(request)
store=RuntimeStore(Path(tempfile.mkdtemp(prefix='eimemory-audit-eval-route-')))
source=ObservedSQLiteSource(store)
with Runtime(store,candidate_source=source) as rt:
    scope=ScopeRef(agent_id='audit-agent',workspace_id='audit-workspace',user_id='audit-user')
    rec=RecordEnvelope.create(kind='memory',title='Certificate rotation schedule',summary='Certificate rotation schedule is sixty days.',content={'memory_type':'fact','text':'Certificate rotation schedule is sixty days.'},source='fixture.memory',scope=scope,meta={'force_capture':True})
    rt.store.append(rec)
    direct=rt.memory.recall(query='Certificate rotation schedule',scope=asdict(scope),limit=5)
    before=source.calls
    report=rt.run_memory_eval_ci({'scope':asdict(scope),'threshold':1.0,'cases':[{'case_id':'route','query':'Certificate rotation schedule','expected_record_ids':[rec.record_id]}]})
    print(json.dumps({'live_source_calls':before,'direct_count':len(direct.items),'memory_eval_calls_to_configured_source':source.calls-before,'memory_eval_passed':report['pass_count'],'memory_eval_threshold':report['passed_threshold']}))
