"""Deployment-bound original-input acceptance, never a replacement for natural coverage."""
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from math import isfinite

from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.models.records import RecordEnvelope, ScopeRef
from .original_query_recall import evaluate_original_queries
from .negative_production_query import evaluate_negative_queries

SOURCE = 'eimemory.evaluation.recall_companion'
SCHEMA = 'production_recall_companion.v1'


def engine_contract(runtime):
    """Bind durable configuration/generation, not process-local warmup flags."""
    engine = runtime.memory.recall_engine
    value = engine.effective_identity()
    value.pop('identity_digest', None)
    source = value.get('candidate_source', {})
    if 'postgres' in source:
        state = engine.candidate_source.repository.read_index_state()
        source['postgres'] = {'committed_watermark':state.watermark,
                             'index_revision':state.authority_revision,
                             'ready':state.ready}
    return value


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def quality_reasons(positive, negative):
    reasons = []
    samples = positive.get('samples', [])
    negatives = negative.get('samples', [])
    counts = Counter(s.get('channel') for s in samples)
    if any(counts[c] < 5 for c in ('codex', 'hermes', 'openclaw')):
        reasons.append('original_query_channel_coverage_missing')
    if len(negatives) < 20:
        reasons.append('natural_negative_coverage_missing')
    if not samples or not all(s.get('online_context_reconstructed') is True for s in samples + negatives):
        reasons.append('original_context_not_reconstructed')
    if any(s.get('unavailable') is not False for s in samples + negatives):
        reasons.append('retrieval_unavailable')
    for channel in ('codex', 'hermes', 'openclaw'):
        rows = [s for s in samples if s.get('channel') == channel]
        if not rows:
            continue
        for name, threshold in (('recall_at_5', .9), ('reciprocal_rank', .8)):
            values = [s.get('rerun', {}).get(name) for s in rows]
            if any(type(v) not in (int, float) or not isfinite(v) for v in values) or sum(values) / len(values) < threshold:
                reasons.append('original_query_quality_failed:' + channel + ':' + name)
    if negatives and sum(s.get('rerun_false_recall') is not False for s in negatives) / len(negatives) > .05:
        reasons.append('natural_negative_false_recall_exceeded')
    times = [s.get('latency_ms') for s in samples + negatives]
    if any(type(t) not in (int, float) or not isfinite(t) or t < 0 for t in times):
        reasons.append('latency_invalid')
    elif times:
        from .metrics import percentile
        if percentile(times, 95) > 3000:
            reasons.append('original_query_latency_exceeded')
    return reasons


def _authority(runtime, positive_ids, negative_ids):
    """Hash referenced live authority, not titles or label-biased ranking aliases."""
    refs = set(positive_ids + negative_ids)
    records = {}
    for _ in range(3):
        for ref in sorted(refs - records.keys()):
            record = runtime.store.get_by_id(ref)
            if record is None or record.status != 'active':
                raise ValueError('companion_authority_stale')
            records[ref] = record.to_dict()
            if record.source in (SOURCE,):
                raise ValueError('companion_recursive_authority')
            # Pending evidence describes historical results, not current gold.
            if record.source in ('eimemory.production_recall.accepted_case',
                    'eimemory.production_recall.label_evidence',
                    'eimemory.production_recall.no_evidence_label'):
                refs.update(record.evidence)
            case = record.content.get('case', {})
            refs.update(label.get('provenance', {}).get('evidence_ref', '') for label in case.get('labels', []))
        refs.discard('')
    if refs - records.keys():
        raise ValueError('companion_authority_depth_exceeded')
    return digest(records)


def run_recall_companion(runtime, *, scope, positive_cases, negative_cases):
    exact = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    release = current_release_identity(runtime, exact)
    if release is None or not release.complete:
        raise ValueError('release_identity_unavailable')
    positive_ids = [c['accepted_record_id'] for c in positive_cases]
    negative_ids = [c['label_record_id'] for c in negative_cases]
    authority = _authority(runtime, positive_ids, negative_ids)
    engine = engine_contract(runtime)
    positive = evaluate_original_queries(runtime, scope=asdict(exact), cases=positive_cases)
    negative = evaluate_negative_queries(runtime, scope=asdict(exact), cases=negative_cases)
    if authority != _authority(runtime, positive_ids, negative_ids):
        raise ValueError('companion_authority_changed')
    if engine != engine_contract(runtime):
        raise ValueError('companion_engine_changed')
    current = current_release_identity(runtime, exact)
    if current is None or release_identity_payload(current) != release_identity_payload(release):
        raise ValueError('companion_release_changed')
    report = {'schema':SCHEMA, 'created_at':now_iso(), 'release_identity':release_identity_payload(release),
        'engine_identity':engine, 'authority_digest':authority,
        'positive_record_ids':positive_ids, 'negative_record_ids':negative_ids,
        'positive':positive, 'negative':negative, 'reasons':quality_reasons(positive, negative)}
    report['passed'] = not report['reasons']
    record = RecordEnvelope.create(kind='reflection', title='Original-query release companion',
        summary='Deployment-bound original-input and natural-negative evaluation.',
        source=SOURCE, scope=exact, content={'report':report},
        meta={'release_commit':release.commit, 'report_digest':digest(report)})
    runtime.store.append(record)
    return {**report, 'record_id':record.record_id}


def verify_recall_companion(runtime, *, scope, release):
    record = runtime.store.latest_record_by_meta_value_exact_scope(kind='reflection', source=SOURCE,
        status='active', scope=scope, meta_key='release_commit', meta_value=release.commit)
    reason = 'current_release_original_companion_missing'
    if record is not None:
        try:
            report = record.content['report']
            if digest(report) != record.meta.get('report_digest'):
                raise ValueError('companion_report_tampered')
            if report['release_identity'] != release_identity_payload(release):
                raise ValueError('companion_release_mismatch')
            receipt = runtime.store.get_by_id(release.receipt_id, scope=scope)
            if receipt is None or record.time.created_at < receipt.time.created_at:
                raise ValueError('companion_report_predeploy')
            if report['authority_digest'] != _authority(runtime, report['positive_record_ids'], report['negative_record_ids']):
                raise ValueError('companion_authority_stale')
            if report['engine_identity'] != engine_contract(runtime):
                raise ValueError('companion_engine_changed')
            reasons = quality_reasons(report['positive'], report['negative'])
            if reasons or report['passed'] is not True:
                raise ValueError(reasons[0] if reasons else 'companion_quality_failed')
            return {'ok':True, 'status':'accepted', 'record_id':record.record_id}
        except (KeyError, TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'companion_report_invalid'
    return {'ok':False, 'status':'blocked', 'reason':reason, 'record_id':record.record_id if record else ''}
