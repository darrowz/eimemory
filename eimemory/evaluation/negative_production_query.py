"""Operator-labelled natural no-evidence cases; never fabricate a positive label.

This companion gate is separate from the existing positive ranking/coverage
contract. Its absence cannot be interpreted as zero false positives.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from time import perf_counter

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef
from .production_query_dataset import pending_production_query_capture_validation_error
from .real_query_gate import PRODUCTION_REAL_QUERY_TRUSTED_LABELERS

SCHEMA = 'production_no_evidence_label.v1'
SOURCE = 'eimemory.production_recall.no_evidence_label'


def _label_id(content):
    return 'prqn_' + sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:32]


def accept_negative_query(runtime, *, pending_record_id, packet, packet_evidence, operator_scope):
    if (not isinstance(packet,dict) or set(packet) != {'query','labeler','reason'}
            or packet['labeler'] not in PRODUCTION_REAL_QUERY_TRUSTED_LABELERS
            or not isinstance(packet['query'],str) or not 1 <= len(packet['query']) <= 16000
            or not isinstance(packet['reason'],str) or not 10 <= len(packet['reason']) <= 1000
            or packet_evidence.get('schema') != 'secure_dataset_fingerprint.v1'
            or not re.fullmatch(r'[a-f0-9]{64}',str(packet_evidence.get('digest') or packet_evidence.get('sha256') or ''))
            or type(packet_evidence.get('size')) is not int or packet_evidence['size'] <= 0):
        raise ValueError('negative_label_packet_invalid')
    pending = runtime.store.get_by_id(pending_record_id)
    if pending is None:
        raise ValueError('negative_pending_missing')
    channel = pending.content.get('channel','')
    exact = ScopeRef.from_dict(resolve_channel_scope(channel,operator_scope))
    reason = pending_production_query_capture_validation_error(runtime,pending,exact_scope=exact,channel=channel)
    if reason:
        raise ValueError(reason)
    digest = sha256(packet['query'].strip().encode()).hexdigest()
    if digest != pending.content['capture_query_digest']:
        raise ValueError('negative_original_query_digest_mismatch')
    content = {'schema':SCHEMA,'pending_record_id':pending.record_id,'channel':channel,
        'query_digest':digest,'source_id':pending.source_id,'scope':asdict(exact),
        'expected':'no_evidence','labeler':packet['labeler'],
        'reason_digest':sha256(packet['reason'].encode()).hexdigest(),
        'packet_digest':packet_evidence.get('digest') or packet_evidence['sha256']}
    record = RecordEnvelope.create(kind='evaluation_packet',title=f'Natural no-evidence label {channel}',
        summary='Operator-reviewed natural negative; not positive ranking coverage.',
        content=content,source=SOURCE,source_id=pending.source_id,scope=exact,evidence=[pending.record_id],
        meta={'report_type':SCHEMA,'natural_positive_coverage':False})
    record.record_id = _label_id(content)
    if runtime.store.get_by_exact_ref(record.record_id,scope=exact,source_id=pending.source_id) is None:
        runtime.store.append(record)
    return {'ok':True,'record_id':record.record_id,'evaluation_role':'natural_negative_label',
            'positive_coverage_increment':0}


def evaluate_negative_queries(runtime, *, scope, cases):
    if not isinstance(cases,list) or not 1 <= len(cases) <= 500:
        raise ValueError('negative_cases_invalid')
    prepared, seen = [], set()
    for case in cases:
        if not isinstance(case,dict) or set(case) != {'label_record_id','query'} or case['label_record_id'] in seen:
            raise ValueError('negative_case_invalid')
        seen.add(case['label_record_id'])
        label = runtime.store.get_by_id(case['label_record_id'])
        if label is None or label.status != 'active' or label.source != SOURCE or label.kind != 'evaluation_packet':
            raise ValueError('negative_label_missing')
        c = label.content
        exact = ScopeRef.from_dict(resolve_channel_scope(c['channel'],scope))
        if (c.get('schema') != SCHEMA or c.get('expected') != 'no_evidence'
                or c.get('labeler') not in PRODUCTION_REAL_QUERY_TRUSTED_LABELERS
                or label.record_id != _label_id(c) or not same_scope(label.scope,exact)
                or not same_scope(ScopeRef.from_dict(c['scope']),exact) or c['source_id'] != label.source_id
                or label.evidence != [c['pending_record_id']]
                or not isinstance(case['query'],str)
                or sha256(case['query'].strip().encode()).hexdigest() != c['query_digest']):
            raise ValueError('negative_label_authority_invalid')
        pending = runtime.store.get_by_exact_ref(c['pending_record_id'],scope=exact,source_id=label.source_id)
        if pending is None:
            raise ValueError('negative_pending_missing')
        reason = pending_production_query_capture_validation_error(runtime,pending,exact_scope=exact,channel=c['channel'])
        if reason or pending.content['capture_query_digest'] != c['query_digest']:
            raise ValueError(reason or 'negative_capture_digest_mismatch')
        prepared.append((case,label,pending,exact))
    samples = []
    for case,label,pending,exact in prepared:
        from .query_input_vault import load_query_input
        original_input = None
        try:
            original_input = load_query_input(runtime, decision_id=pending.content['capture_ref'],
                scope=exact, channel=label.content['channel'], source_id=label.source_id)
        except ValueError as exc:
            if str(exc) not in {'original_query_input_unavailable', 'original_query_input_boundary_mismatch'}:
                raise
        start = perf_counter()
        bundle = runtime.memory.recall(
            query=original_input['effective_query'] if original_input else case['query'].strip(),
            scope=asdict(exact), limit=original_input['limit'] if original_input else 5,
            task_context=dict(original_input['task_context']) if original_input else {
                'exact_scope_only':True,'source_ids':[label.source_id],
                'target_source_id':label.source_id,'runtime_channel':label.content['channel']})
        if any(not same_scope(item.scope, exact) or item.source_id != label.source_id for item in bundle.items):
            raise ValueError('negative_query_rerun_boundary_violation')
        unavailable = bundle.explanation.get('retrieval_status') == 'unavailable'
        samples.append({'label_record_id':label.record_id,'channel':label.content['channel'],
            'query_digest':label.content['query_digest'],'observed_false_recall':bool(pending.content['candidate_refs']),
            'rerun_false_recall':bool(bundle.items),'unavailable':unavailable,
            'online_context_reconstructed':bool(original_input and not original_input['external_bundle']
                and original_input.get('identity_schema') == 'proactive-query-identity.v2'),
            'input_digest':original_input['input_digest'] if original_input else '',
            'passed':not bundle.items and not unavailable,'latency_ms':round((perf_counter()-start)*1000,3)})
    rate = sum(s['rerun_false_recall'] for s in samples)/len(samples)
    return {'schema':'production_no_evidence_report.v1','created_at':now_iso(),
        'evaluation_role':'natural_negative_engine_rerun',
        'online_context_reconstructed':all(s['online_context_reconstructed'] for s in samples),
        'natural_positive_gate_replacement':False,'case_count':len(samples),'false_recall_rate':rate,
        'engine_identity':runtime.memory.recall_engine.effective_identity(),'samples':samples,
        'passed':rate <= .05 and not any(s['unavailable'] for s in samples)}
