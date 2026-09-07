"""Original-query semantic acceptance, including negatives and returned precision.

Never seeds production, manufactures natural samples, or replaces the formal
production report. Expected groups represent alternative records for one fact.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef
from .metrics import percentile

THRESHOLDS = {'hit_at_1':0.90,'hit_at_5':0.90,'false_recall_rate':0.05,
              'returned_precision':0.90,'forbidden_hit_count':0,'unavailable_count':0,'latency_ms_p95':3000.0}


def validate_dataset(dataset):
    if not isinstance(dataset,dict) or dataset.get('schema') != 'semantic_recall_cases.v1':
        raise ValueError('semantic_dataset_schema_invalid')
    cases = dataset.get('cases')
    if not isinstance(cases,list) or not 1 <= len(cases) <= 1000:
        raise ValueError('semantic_dataset_bounds')
    if any(not isinstance(case,dict) for case in cases):
        raise ValueError('semantic_case_invalid')
    cases = [{'scope':dataset.get('scope'), 'source_id':dataset.get('source_id'), **case} for case in cases]
    seen, partitions = set(), {}
    for case in cases:
        if not isinstance(case,dict):
            raise ValueError('semantic_case_invalid')
        case_id, query = case.get('case_id'), case.get('query')
        if (not isinstance(case_id,str) or not case_id or case_id in seen
                or not isinstance(query,str) or not 1 <= len(query) <= 16000
                or case.get('split') not in {'development','holdout','regression'}
                or not isinstance(case.get('intent_group'),str) or not case['intent_group']
                or not isinstance(case.get('scope'),dict) or not case.get('source_id')):
            raise ValueError('semantic_case_identity_invalid')
        seen.add(case_id)
        group, split = case['intent_group'],case['split']
        if group in partitions and partitions[group] != split:
            raise ValueError('semantic_intent_split_leakage')
        partitions[group] = split
        groups = case.get('expected_groups')
        if not isinstance(groups,list) or any(not isinstance(g,list) or not g
                or any(not isinstance(ref,str) or not ref for ref in g) for g in groups):
            raise ValueError('semantic_expected_groups_invalid')
        refs = [ref for group in groups for ref in group]
        if len(refs) != len(set(refs)):
            raise ValueError('semantic_expected_groups_overlap')
        forbidden = case.get('forbidden_refs',[])
        if not isinstance(forbidden,list) or any(not isinstance(ref,str) for ref in forbidden) or set(refs)&set(forbidden):
            raise ValueError('semantic_forbidden_refs_invalid')
    return cases


def score_case(refs, groups, *, forbidden_refs=(), unavailable=False, boundary_violation=False):
    refs = list(dict.fromkeys(refs))[:5]
    gold = {ref for group in groups for ref in group}
    hits = [i for i,ref in enumerate(refs,1) if ref in gold]
    forbidden = len(set(refs)&set(forbidden_refs)) + int(boundary_violation)
    return {'hit_at_1':bool(hits and hits[0] == 1) if groups else None,
        'hit_at_5':bool(hits) if groups else None,
        'group_recall':sum(bool(set(group)&set(refs)) for group in groups)/len(groups) if groups else None,
        'returned_relevant_count':len(hits),'returned_count':len(refs),
        'false_recall':bool(refs) if not groups else None,
        'forbidden_hit_count':forbidden,'unavailable':bool(unavailable),
        'passed':not unavailable and not forbidden and
            ((bool(hits) and hits[0] == 1 and len(hits) == len(refs)
              and all(set(group)&set(refs) for group in groups)) if groups else not refs)}


def summarize(samples):
    positives = [s for s in samples if s['metrics']['hit_at_1'] is not None]
    negatives = [s for s in samples if s['metrics']['false_recall'] is not None]
    returned = sum(s['metrics']['returned_count'] for s in samples)
    metrics = {'hit_at_1':sum(s['metrics']['hit_at_1'] for s in positives)/len(positives) if positives else None,
        'hit_at_5':sum(s['metrics']['hit_at_5'] for s in positives)/len(positives) if positives else None,
        'false_recall_rate':sum(s['metrics']['false_recall'] for s in negatives)/len(negatives) if negatives else None,
        'returned_precision':sum(s['metrics']['returned_relevant_count'] for s in samples)/returned if returned else None,
        'forbidden_hit_count':sum(s['metrics']['forbidden_hit_count'] for s in samples),
        'unavailable_count':sum(s['metrics']['unavailable'] for s in samples),
        'latency_ms_p95':percentile([s['latency_ms'] for s in samples],95)}
    failures = []
    for name, threshold in THRESHOLDS.items():
        value = metrics[name]
        minimum = name in {'hit_at_1','hit_at_5','returned_precision'}
        if value is None or (value < threshold if minimum else value > threshold):
            failures.append(name)
    return {'case_count':len(samples),'positive_count':len(positives),'negative_count':len(negatives),
            'metrics':metrics,'failed_gates':failures,'passed':not failures}


def evaluate_semantic_recall(runtime, dataset):
    cases = validate_dataset(dataset)
    # Validate every label before running the first query. This is not seeding.
    for case in cases:
        exact = ScopeRef.from_dict(case['scope'])
        for group in case['expected_groups']:
            for ref in group:
                record = runtime.store.get_by_exact_ref(ref,scope=exact,source_id=case['source_id'])
                if record is None or record.status != 'active':
                    raise ValueError('semantic_label_authority_missing')
    samples = []
    for case in cases:
        context = dict(case.get('task_context') or {})
        context.update({'exact_scope_only':True,'source_ids':[case['source_id']]})
        started = perf_counter()
        bundle = runtime.memory.recall(query=case['query'],scope=case['scope'],limit=5,task_context=context)
        elapsed = (perf_counter()-started)*1000
        refs = [item.record_id for item in bundle.items]
        boundary = any(asdict(item.scope) != asdict(ScopeRef.from_dict(case['scope']))
            or item.source_id != case['source_id'] or item.status != 'active' for item in bundle.items)
        selector = bundle.explanation.get('relevance_selector',{})
        samples.append({'case_id':case['case_id'],'split':case['split'],
            'query_digest':sha256(case['query'].encode()).hexdigest(),'result_refs':refs,
            'latency_ms':round(elapsed,3),'retrieval_status':bundle.explanation.get('retrieval_status','unknown'),
            'admission':selector,
            'metrics':score_case(refs,case['expected_groups'],forbidden_refs=case.get('forbidden_refs',[]),
                unavailable=selector.get('status') == 'unavailable',boundary_violation=boundary)})
    splits = {split:summarize([s for s in samples if s['split'] == split])
              for split in sorted({s['split'] for s in samples})}
    regression = [s for s in samples if s['split'] == 'regression']
    holdout = splits.get('holdout',{})
    passed = (len(cases) >= 60 and bool(holdout.get('passed'))
              and holdout.get('positive_count',0) >= 20 and holdout.get('negative_count',0) >= 20
              and bool(regression) and all(s['metrics']['passed'] for s in regression)
              and not any(s['metrics']['forbidden_hit_count'] for s in samples))
    return {'schema':'semantic_admission_acceptance.v1','created_at':now_iso(),
        'evaluation_role':'acceptance_only','natural_gate_replacement':False,
        'dataset_digest':sha256(json.dumps(dataset,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        'engine_identity':runtime.memory.recall_engine.effective_identity(),
        'thresholds':THRESHOLDS,'summary':summarize(samples),'splits':splits,'samples':samples,
        'minimum_case_count':60,'passed':passed,'ok':passed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    from eimemory.api.runtime import Runtime
    from eimemory.scheduler.jobs import load_json_dataset_with_evidence
    dataset, evidence = load_json_dataset_with_evidence(args.dataset)
    runtime = Runtime.create(root=args.root)
    try:
        report = evaluate_semantic_recall(runtime,dataset)
        report['secure_dataset_evidence'] = evidence
        output = Path(args.output)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k not in {'samples','engine_identity'}},ensure_ascii=True))
        return 0 if report['passed'] else 1
    finally:
        runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
