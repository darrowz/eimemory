import sys, os, tempfile, json
from pathlib import Path
sys.path[:0]=['/workspace/eimemory','/workspace/eimemory/tests']
from eimemory.api.runtime import Runtime
from test_production_query_auto_review import _seed,_collect,BASE_SCOPE,CHANNEL,auto_review_pending_production_queries
os.environ['EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY']='audit-local-only-0123456789-abcdefghijklmnopqrstuvwxyz'
os.environ['EIMEMORY_CAPTURE_ORIGINAL_QUERY']='1'
os.environ.pop('EIMEMORY_CAPTURE_QUERY_SCOPES',None)
with Runtime.create(root=Path(tempfile.mkdtemp(prefix='eimemory-audit-default-window-'))) as rt:
    _seed(rt,94000,with_candidate=False)
    oldest=_collect(rt)[0]
    for index in range(94001,94501):
        _seed(rt,index,with_candidate=False)
    latest=_collect(rt)
    reports=[auto_review_pending_production_queries(rt,scope=BASE_SCOPE) for _ in range(2)]
    reviewed={v['pending_record_id'] for r in reports for v in r['review_results']}
    print(json.dumps({'captured_pending':len(set(latest+[oldest])), 'scanned_per_run':[r['scanned_count'] for r in reports], 'distinct_reviewed_two_runs':len(reviewed), 'oldest_reviewed':oldest in reviewed, 'open_review_count':[r['open_review_count'] for r in reports], 'status':[r['status'] for r in reports], 'truncation_fields':[{k:v for k,v in r.items() if 'truncat' in k or 'remaining' in k} for r in reports]}))
