import sys,tempfile,json
from pathlib import Path
from dataclasses import asdict
sys.path.insert(0,'/workspace/eimemory')
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef, RecordEnvelope
from eimemory.scheduler.jobs import _run_production_recall_eval
from eimemory.scheduler.result_contract import nightly_result_diagnostics
root=Path(tempfile.mkdtemp(prefix='eimemory-audit-nightly-gate-'))
with Runtime.create(root=root) as rt:
    scope=ScopeRef(agent_id='audit-agent',workspace_id='audit-workspace',user_id='audit-user')
    record=RecordEnvelope.create(kind='memory',title='Certificate rotation schedule',summary='Certificate rotation schedule is sixty days.',content={'memory_type':'fact','text':'Certificate rotation schedule is sixty days.'},source='fixture.memory',scope=scope,meta={'force_capture':True})
    rt.store.append(record)
    evaluation=root/'evaluation'
    evaluation.mkdir(mode=0o700,exist_ok=True)
    dataset=evaluation/'production_recall.json'
    dataset.write_text(json.dumps({'scope':asdict(scope),'cases':[{'case_id':f'audit-{i}','query':'Certificate rotation schedule','expected_record_ids':[record.record_id],'scope':asdict(scope),'topk':5} for i in range(10)]}))
    dataset.chmod(0o600)
    report=_run_production_recall_eval(rt,scope=asdict(scope))
    gate=report.get('quality_gate',{})
    diagnostics=nightly_result_diagnostics({'recall_quality_gate':gate},[])
    print(json.dumps({'dataset_source':report.get('dataset_source'),'sample_count':report.get('sample_count'),'pass_count':report.get('pass_count'),'contract':report.get('evaluation_contract'),'gate_ok':gate.get('ok'),'recall_quality_accepted':diagnostics['recall_quality_accepted'],'evidence':gate.get('recall_quality_evidence'),'error':report.get('error'),'detail':report.get('detail')}))
