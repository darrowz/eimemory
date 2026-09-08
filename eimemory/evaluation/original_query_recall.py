"""Digest-verified original-query reruns, separate from the redacted gate.

Raw queries are supplied in a private operator packet, never persisted here.
This engine-level rerun cannot reconstruct missing conversation context or
retroactively replace the response that a production caller actually received.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from time import perf_counter

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import ScopeRef
from eimemory.core.clock import now_iso
from eimemory.version import __version__
from .production_query_dataset import accepted_production_query_validation_error


def _metrics(refs, labels):
    refs = list(dict.fromkeys(refs))[:5]
    gold = {label['record_ref'] for label in labels}
    hits = [i for i, ref in enumerate(refs, 1) if ref in gold]
    return {'recall_at_5': len(hits) / len(gold) if gold else None,
            'precision_at_5': len(hits) / 5,
            'returned_precision': len(hits) / len(refs) if refs else None,
            'false_recall': bool(refs) if not gold else None,
            'reciprocal_rank': 1 / hits[0] if hits else 0.0, 'result_refs': refs}


def evaluate_original_queries(runtime, *, scope, cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
        raise ValueError('original_query_cases_invalid')
    prepared, seen = [], set()
    # Validate the entire packet before any rerun, including duplicates.
    for entry in cases:
        if not isinstance(entry, dict) or set(entry) != {'channel', 'accepted_record_id', 'query'}:
            raise ValueError('original_query_case_fields_invalid')
        channel, record_id, query = (entry[k] for k in ('channel', 'accepted_record_id', 'query'))
        if (not isinstance(channel, str) or channel not in SUPPORTED_RUNTIME_CHANNELS
                or not isinstance(record_id, str) or not record_id or record_id in seen
                or not isinstance(query, str) or not query.strip() or len(query) > 16000):
            raise ValueError('original_query_case_invalid')
        seen.add(record_id)
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, scope))
        accepted = runtime.store.get_by_id(record_id, scope=exact)
        if accepted is None:
            raise ValueError('original_query_accepted_missing')
        reason = accepted_production_query_validation_error(runtime, accepted, exact_scope=exact, channel=channel)
        if reason:
            raise ValueError(reason)
        pending = runtime.store.get_by_id(accepted.evidence[0], scope=exact)
        digest = sha256(query.strip().encode('utf-8')).hexdigest()
        if digest != pending.content['capture_query_digest']:
            raise ValueError('original_query_digest_mismatch')
        with runtime.store._lock:
            decision = runtime.store.sqlite.conn.execute(
                'SELECT task_type,effective_query_digest FROM proactive_decisions WHERE decision_id=?',
                (pending.content['capture_ref'],)).fetchone()
        prepared.append((entry, accepted.content['case'], pending.content, exact, dict(decision), digest))
    samples = []
    for entry, case, capture, exact, decision, digest in prepared:
        from .query_input_vault import load_query_input
        original_input = None
        try:
            original_input = load_query_input(runtime,decision_id=capture['capture_ref'],
                scope=exact,channel=entry['channel'],source_id=case['source_id'])
        except ValueError as exc:
            if str(exc) not in {'original_query_input_unavailable','original_query_input_boundary_mismatch'}:
                raise
        start = perf_counter()
        query = original_input['effective_query'] if original_input else entry['query'].strip()
        context = dict(original_input['task_context']) if original_input else {
                          'source_ids':[case['source_id']], 'target_source_id':case['source_id'],
                          'runtime_channel':entry['channel'], 'task_type':decision['task_type'],
                          'exact_scope_only':True}
        bundle = runtime.memory.recall(query=query, scope=asdict(exact),
            limit=original_input['limit'] if original_input else 5, task_context=context)
        items = list(bundle.items)
        unavailable = (getattr(bundle, 'explanation', {}) or {}).get('retrieval_status') == 'unavailable'
        if any(not same_scope(item.scope, exact) or item.source_id != case['source_id'] for item in items):
            raise ValueError('original_query_rerun_boundary_violation')
        samples.append({'accepted_record_id':entry['accepted_record_id'], 'channel':entry['channel'],
            'capture_ref':capture['capture_ref'], 'query_digest':digest,
            'online_context_reconstructed':bool(original_input and not original_input['external_bundle']
                and original_input.get('identity_schema') == 'proactive-query-identity.v2'),
            'unavailable':unavailable,
            'input_digest':original_input['input_digest'] if original_input else '',
            'context_rewrite_observed': (
                original_input['effective_query'] != original_input['query'] if original_input else None),
            'observed':_metrics(list(capture['candidate_refs']), case['labels']),
            'rerun':_metrics([item.record_id for item in items], case['labels']),
            'latency_ms':round((perf_counter()-start)*1000, 3)})
    return {'ok':True, 'schema':'production_original_query_rerun.v1',
            'created_at':now_iso(), 'evaluator_version':__version__,
            'engine_identity':runtime.memory.recall_engine.effective_identity(),
            'evaluation_role':'supplemental_engine_rerun', 'natural_gate_replacement':False,
            'online_context_reconstructed':all(s['online_context_reconstructed'] for s in samples),
            'case_count':len(samples), 'samples':samples}
