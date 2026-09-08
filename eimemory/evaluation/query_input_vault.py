"""Opt-in, private, bounded inputs for faithful proactive-query replay.

Disabled by default. Raw text never goes into records, exports, diagnostics or
natural labels. A hash is not enough to reconstruct the original online request.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os

from eimemory.models.records import ScopeRef
from eimemory.retrieval.query_identity import (
    QUERY_IDENTITY_SCHEMA, effective_query_digest, query_text_digest,
)


def capture_query_input(runtime, *, decision_id, query, effective_query, explanation,
                        external_bundle=False):
    if os.environ.get('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0') != '1':
        return {'status':'disabled'}
    if not all(isinstance(q,str) and 0 < len(q) <= 16000 for q in (query,effective_query)):
        return {'status':'input_bounds_rejected'}
    query_digest = query_text_digest(query)
    with runtime.store._lock:
        conn = runtime.store.sqlite.conn
        decision = conn.execute('SELECT query_digest,effective_query_digest,source_ids_json,task_type '
            'FROM proactive_decisions WHERE decision_id=?',(decision_id,)).fetchone()
        if (decision is None or decision['query_digest'] != query_digest
                or decision['effective_query_digest'] != effective_query_digest(decision['task_type'], effective_query)):
            return {'status':'decision_identity_mismatch'}
        context = dict(explanation.get('task_context') or {})
        context = {k:v for k,v in context.items() if k in {
            'source_ids','runtime_channel','exact_scope_only','task_type','recall_profile',
            'scope_strategy','recall_mode','candidate_limit','intent','target_source_id'}}
        payload = {'identity_schema':QUERY_IDENTITY_SCHEMA,
                   'effective_text_digest':query_text_digest(effective_query),
                   'task_type':decision['task_type'],
                   'query':query,'effective_query':effective_query,'task_context':context,
                   'limit':8,'external_bundle':bool(external_bundle)}
        serialized = json.dumps(payload,ensure_ascii=False,sort_keys=True)
        if len(serialized.encode()) > 65536:
            return {'status':'input_bounds_rejected'}
        conn.execute('CREATE TABLE IF NOT EXISTS proactive_query_input_vault ('
            'decision_id TEXT PRIMARY KEY,payload TEXT NOT NULL,input_digest TEXT NOT NULL,'
            'retrieval_status TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('INSERT OR IGNORE INTO proactive_query_input_vault '
            '(decision_id,payload,input_digest,retrieval_status) VALUES(?,?,?,?)',
            (decision_id,serialized,sha256(serialized.encode()).hexdigest(),
             str(explanation.get('retrieval_status') or 'unknown')[:40]))
        existing = conn.execute('SELECT input_digest FROM proactive_query_input_vault WHERE decision_id=?',
                                (decision_id,)).fetchone()
        if existing['input_digest'] != sha256(serialized.encode()).hexdigest():
            return {'status':'input_identity_conflict'}
        # Deletes only this optional private cache, never decision/label authority.
        conn.execute("DELETE FROM proactive_query_input_vault WHERE created_at<datetime('now','-30 days') "
                     'OR decision_id NOT IN (SELECT decision_id FROM proactive_decisions)')
        conn.execute('DELETE FROM proactive_query_input_vault WHERE rowid IN ('
            'SELECT rowid FROM proactive_query_input_vault ORDER BY rowid DESC LIMIT -1 OFFSET 10000)')
        conn.commit()
    return {'status':'captured','input_digest':sha256(serialized.encode()).hexdigest()}


def load_query_input(runtime, *, decision_id, scope, channel, source_id):
    exact = asdict(ScopeRef.from_dict(scope)) if isinstance(scope,dict) else asdict(scope)
    with runtime.store._lock:
        conn = runtime.store.sqlite.conn
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_query_input_vault'").fetchone():
            raise ValueError('original_query_input_unavailable')
        row = conn.execute('SELECT v.payload,v.input_digest,v.retrieval_status,d.* '
            'FROM proactive_query_input_vault v JOIN proactive_decisions d USING(decision_id) '
            "WHERE decision_id=? AND v.created_at>=datetime('now','-30 days')",(decision_id,)).fetchone()
    if (row is None or row['channel'] != channel or any(row[k] != v for k,v in exact.items())
            or json.loads(row['source_ids_json']) != [source_id]):
        raise ValueError('original_query_input_boundary_mismatch')
    payload = json.loads(row['payload'])
    schema = payload.get('identity_schema')
    effective_digest = effective_query_digest(row['task_type'], payload['effective_query'])
    if schema is None:
        # Old private payloads remain readable, never rewritten as v2 evidence.
        effective_digest = query_text_digest(payload['effective_query'])
    elif (schema != QUERY_IDENTITY_SCHEMA or payload.get('task_type') != row['task_type']
          or payload.get('effective_text_digest') != query_text_digest(payload['effective_query'])):
        raise ValueError('original_query_input_digest_mismatch')
    if (sha256(row['payload'].encode()).hexdigest() != row['input_digest']
            or query_text_digest(payload['query']) != row['query_digest']
            or effective_digest != row['effective_query_digest']):
        raise ValueError('original_query_input_digest_mismatch')
    return {**payload,'identity_schema':schema or 'legacy-plain-query.v1',
            'input_digest':row['input_digest'],'retrieval_status':row['retrieval_status']}


def capture_pipeline_status(runtime, *, scope):
    """Counts each collection barrier without disclosing original requests."""
    from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
    base = asdict(scope) if isinstance(scope,ScopeRef) else scope
    result = {}
    with runtime.store._lock:
        conn = runtime.store.sqlite.conn
        for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
            exact = resolve_channel_scope(channel,base)
            rows = conn.execute('SELECT release_bound,control_cohort,task_type,source_ids_json,COUNT(*) AS count '
                'FROM proactive_decisions WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? '
                'GROUP BY release_bound,control_cohort,task_type,source_ids_json',
                (channel,exact['tenant_id'],exact['agent_id'],exact['workspace_id'],exact['user_id'])).fetchall()
            counts = {'total':0,'release_unbound':0,'control':0,'unclassified':0,'non_exact_source':0,'eligible_decisions':0}
            for row in rows:
                n = row['count']
                counts['total'] += n
                reason = ('release_unbound' if not row['release_bound'] else 'control' if row['control_cohort']
                    else 'unclassified' if not row['task_type'] else 'non_exact_source'
                    if len(json.loads(row['source_ids_json'])) != 1 or json.loads(row['source_ids_json']) == ['*']
                    else 'eligible_decisions')
                counts[reason] += n
            result[channel] = counts
    return {'schema':'production_capture_pipeline.v1','channels':result,'natural_quality_proven':False}
