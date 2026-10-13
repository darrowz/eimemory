import sys, os, tempfile, json
from pathlib import Path
sys.path[:0] = ['/workspace/eimemory', '/workspace/eimemory/tests']
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef, RecordEnvelope
from eimemory.evaluation.production_recall import run_production_recall_eval, evaluate_production_recall_quality_gate, RECALL_QUALITY_GATE_THRESHOLDS
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_production_query_auto_review import _seed, _collect, BASE_SCOPE, CHANNEL, auto_review_pending_production_queries, resolve_channel_scope
os.environ['EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY'] = 'audit-local-only-0123456789-abcdefghijklmnopqrstuvwxyz'
os.environ['EIMEMORY_CAPTURE_ORIGINAL_QUERY'] = '1'
os.environ.pop('EIMEMORY_CAPTURE_QUERY_SCOPES', None)
for contract in ('legacy.v1', 'not-a-supported-contract', 'judged_relevance.v1', 'known_item_smoke.v1'):
    report={**RECALL_QUALITY_GATE_THRESHOLDS,'sample_count':10,'evaluation_contract':contract,'cross_channel_leakage_count':0,'source_filter_leakage_count':0}
    gate=evaluate_production_recall_quality_gate(report)
    diag=nightly_result_diagnostics({'recall_quality_gate':gate}, [])
    print(json.dumps({'probe':'contract_authority','contract':contract,'gate_ok':gate['ok'],'trusted':gate['recall_quality_evidence']['label_trust_accepted'],'release_authority':gate['recall_quality_evidence']['release_authority_verified'],'nightly_accepted':diag['recall_quality_accepted']}))
with Runtime.create(root=Path(tempfile.mkdtemp(prefix='eimemory-audit-cjk-'))) as rt:
    _seed(rt,93001,query='轮换证书需要遵循哪些具体步骤')
    _collect(rt)
    report=auto_review_pending_production_queries(rt,scope=BASE_SCOPE)
    row=report['review_results'][0]
    print(json.dumps({'probe':'chinese_natural_review','accepted':report['accepted_count'],'reasons':row['reasons'],'signals':row['signal_counts'],'semantic':row['semantic_quality']}, ensure_ascii=False))
with Runtime.create(root=Path(tempfile.mkdtemp(prefix='eimemory-audit-review-window-'))) as rt:
    _seed(rt,93002,with_candidate=False)
    _seed(rt,93003,with_candidate=False)
    ids=_collect(rt)
    reports=[auto_review_pending_production_queries(rt,scope=BASE_SCOPE,limit=1) for _ in range(3)]
    reviewed={r['pending_record_id'] for report in reports for r in report['review_results']}
    print(json.dumps({'probe':'review_window','captured':len(ids),'distinct_reviewed_three_runs':len(reviewed),'never_reviewed':len(set(ids)-reviewed),'open_review_counts':[r['open_review_count'] for r in reports],'statuses':[r['status'] for r in reports]}))
with Runtime.create(root=Path(tempfile.mkdtemp(prefix='eimemory-audit-legacy-native-'))) as rt:
    scope=ScopeRef(agent_id='audit-agent',workspace_id='audit-workspace',user_id='audit-user')
    record=RecordEnvelope.create(kind='memory',title='Certificate rotation schedule',summary='Certificate rotation schedule is sixty days.',content={'memory_type':'fact','text':'Certificate rotation schedule is sixty days.'},source='fixture.memory',scope=scope,meta={'force_capture':True})
    rt.store.append(record)
    from dataclasses import asdict
    cases=[{'case_id':f'audit-{i}','query':'Certificate rotation schedule','expected_record_ids':[record.record_id],'scope':asdict(scope),'topk':5} for i in range(10)]
    report=run_production_recall_eval(rt,{'scope':asdict(scope),'cases':cases},seed=False,persist_report=True)
    gate=report['quality_gate']
    diag=nightly_result_diagnostics({'recall_quality_gate':gate}, [])
    print(json.dumps({'probe':'native_legacy_gate','samples':report['sample_count'],'passed':report['pass_count'],'contract':report['evaluation_contract'],'p_at_3':report['p_at_3'],'gate_ok':gate['ok'],'blocking':gate['blocking_metrics'],'trusted':gate['recall_quality_evidence']['label_trust_accepted'],'release_authority':gate['recall_quality_evidence']['release_authority_verified'],'nightly_accepted':diag['recall_quality_accepted']}))
