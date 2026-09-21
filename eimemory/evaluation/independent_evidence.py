"""Read-only acceptance summaries, not label generation or synthetic gold.

Input JSONL has measured elapsed_ms, correct (independently reviewed), delivered,
retrieval_status, route and optional phase. All requests, including failures,
enter latency and effective-within-budget denominators. Original text is ignored.
"""
from __future__ import annotations
import argparse
import json
import math

REQUIRED = {'elapsed_ms','correct','delivered','retrieval_status','route'}


def summarize(samples):
    rows=list(samples)
    if not rows:
        return {'count':0,'verdict':'unknown','reason':'no_samples'}
    for row in rows:
        if not isinstance(row,dict) or not REQUIRED <= set(row):
            raise ValueError('acceptance_fields_missing')
        n=row['elapsed_ms']
        if type(n) not in (int,float) or not math.isfinite(n) or n<0:
            raise ValueError('acceptance_time_invalid')
        if any(type(row[k]) is not bool for k in ('correct','delivered')):
            raise ValueError('acceptance_label_invalid')
        if row['retrieval_status'] not in {'evidence_found','no_evidence','unavailable'}:
            raise ValueError('acceptance_status_invalid')
        if row['route'] not in {'local','model','negative','unknown'}:
            raise ValueError('acceptance_route_invalid')
    def effective(row):
        return (row['correct'] and row['retrieval_status']!='unavailable'
                and (row['retrieval_status']=='no_evidence' or row['delivered']))
    latencies=sorted(row['elapsed_ms'] for row in rows)
    fast=sum(effective(row) and row['elapsed_ms']<=3000 for row in rows)
    return {'count':len(rows),'within_3s_effective':fast,
        'within_3s_effective_rate':fast/len(rows),
        'effective':sum(effective(row) for row in rows),
        'unavailable':sum(row['retrieval_status']=='unavailable' for row in rows),
        'max_ms':latencies[-1],'mean_ms':sum(latencies)/len(rows),
        'sample_nearest_rank_p95_ms':latencies[math.ceil(.95*len(rows))-1],
        'verdict':'pass' if fast==len(rows) else 'fail',
        'boundary':'sample_only_not_production_slo_or_natural_gold',
        'routes':{r:sum(row['route']==r for row in rows) for r in ('local','model','negative','unknown')}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True)
    a=p.parse_args(argv)
    rows=[]
    with open(a.input,encoding='utf-8') as f:
        for i,line in enumerate(f):
            if i>=10000 or len(line.encode())>65536:
                raise ValueError('acceptance_input_bound')
            if line.strip():
                rows.append(json.loads(line))
    result=summarize(rows)
    result['phases']={phase:summarize([r for r in rows if r.get('phase')==phase])
                      for phase in ('cold','warm') if any(r.get('phase')==phase for r in rows)}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    main()
