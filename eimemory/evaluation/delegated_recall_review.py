"""Local user-delegated review of Codex captures, without positive-label grants.

The protected delegation packet uses the existing local CLI custody model.
The receipt key attests the automatic review, never a human/operator signature.
Pending observations and old quarantine events are not rewritten. Positive
labels still require the independent operator contract.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import re

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_SOURCE, PENDING_SOURCE, accepted_production_query_validation_error,
    pending_production_query_capture_validation_error,
)
from eimemory.evaluation.query_input_vault import load_query_input
from eimemory.evaluation.real_query_gate import _stable_digest
from eimemory.governance.evidence_contract import same_scope
from eimemory.governance.tool_receipts import _receipt_key_set
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import load_json_dataset_with_evidence

SCHEMA = 'production_recall_delegated_review.v1'
SOURCE = 'eimemory.production_recall.delegated_review'
DELEGATION_SCHEMA = 'production_recall_review_delegation.v1'


def _local_principal():
    import os
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def load_review_delegation(path, *, scope, channel):
    """Validate custody and exact authority before collection or review writes."""
    packet, fingerprint = load_json_dataset_with_evidence(str(path))
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
    if not isinstance(packet, dict) or set(packet) != {
            'schema', 'scope', 'channel', 'source_id', 'delegator', 'delegate',
            'actions', 'authorization_ref', 'expires_at'}:
        raise ValueError('review_delegation_fields_invalid')
    authorization = packet.get('authorization_ref')
    if (channel != 'codex' or packet['channel'] != channel
            or packet['schema'] != DELEGATION_SCHEMA
            or packet['scope'] != asdict(exact) or packet['source_id'] != 'codex'
            or not exact.user_id or packet['delegator'] != exact.user_id
            or packet['delegator'] != _local_principal()
            or packet['delegate'] != 'codex' or packet['actions'] != ['review_pending']
            or not isinstance(authorization, dict)
            or set(authorization) != {'kind', 'session_id', 'message_digest'}
            or authorization['kind'] != 'user_instruction'
            or not isinstance(authorization['session_id'], str)
            or not 1 <= len(authorization['session_id']) <= 100
            or re.fullmatch('[a-f0-9]{64}', str(authorization['message_digest'])) is None):
        raise ValueError('review_delegation_authority_invalid')
    try:
        expiry = datetime.fromisoformat(packet['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError('review_delegation_expired')
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('review_delegation_expired') from exc
    return packet, fingerprint, exact


def _signature(body, key):
    return hmac.new(key.encode(), (SCHEMA + ':' + _stable_digest(body)).encode(), sha256).hexdigest()


def verify_delegated_review(record, *, scope):
    if (record is None or record.kind != 'evaluation_packet' or record.status != 'active'
            or record.source != SOURCE or record.source_id != 'codex'
            or not same_scope(record.scope, scope)):
        raise ValueError('review_receipt_boundary_invalid')
    body = dict(record.content)
    signature = str(body.pop('signature', ''))
    keys = _receipt_key_set()
    key = keys.verification_keys.get(body.get('key_id'), '') if keys else ''
    if not key or not hmac.compare_digest(signature, _signature(body, key)):
        raise ValueError('review_receipt_signature_invalid')
    if (body.get('schema') != SCHEMA or body.get('record_id') != record.record_id
            or body.get('scope') != asdict(scope) or body.get('source') != SOURCE
            or body.get('reviewer') != 'codex' or body.get('natural_gold_created') is not False
            or body.get('disposition') not in {'maintenance', 'rejected', 'evidence_insufficient',
                                              'pending_independent_review', 'accepted'}
            or record.evidence != [body.get('pending_record_id')]
            or record.meta.get('report_type') != 'production_recall_delegated_review'):
        raise ValueError('review_receipt_identity_invalid')
    return record.content


def _assessment(runtime, pending, exact, accepted):
    payload = pending.content
    capture_ref = str(payload.get('capture_ref') or '')
    decision = runtime.store.sqlite.conn.execute(
        'SELECT acceptance_generated FROM proactive_decisions WHERE decision_id=?',
        (capture_ref,)).fetchone()
    facts = {'pending_digest': _stable_digest(pending.to_dict()), 'capture_ref': capture_ref,
             'decision_provenance': dict(decision) if decision else None}
    if pending.status == 'quarantined':
        return 'rejected', ['quarantined_evidence'], facts, []
    if payload.get('acceptance_generated') is True or (decision and decision['acceptance_generated'] == 1):
        return 'maintenance', ['maintenance_capture_not_natural'], facts, []
    if not decision or decision['acceptance_generated'] is None:
        return 'evidence_insufficient', ['capture_provenance_unknown' if decision else
                                        'pending_capture_decision_missing'], facts, []
    reason = pending_production_query_capture_validation_error(
        runtime, pending, exact_scope=exact, channel='codex')
    if reason:
        return 'rejected', [reason], facts, []
    reasons = []
    try:
        original = load_query_input(runtime, decision_id=capture_ref, scope=exact,
                                    channel='codex', source_id='codex')
        facts['input_digest'] = original['input_digest']
        facts['retrieval_status'] = original['retrieval_status']
        if 'host_query' not in original:
            reasons.append('original_host_input_missing')
        if original['retrieval_status'] == 'unavailable':
            reasons.append('retrieval_unavailable')
    except ValueError as exc:
        reason = str(exc)
        reasons.append(reason if re.fullmatch('original_[a-z_]{1,80}', reason)
                       else 'original_query_input_invalid')
    valid_accepted = []
    facts['accepted_authority'] = []
    for record in accepted:
        reason = accepted_production_query_validation_error(
            runtime, record, exact_scope=exact, channel='codex')
        facts['accepted_authority'].append({'record_id': record.record_id,
            'digest': _stable_digest(record.to_dict()), 'validation_error': reason})
        if not reason:
            valid_accepted.append(record.record_id)
    # Historical retrieval membership is not positive-label authority: an
    # independent operator may legitimately label an answer that was missed.
    if valid_accepted and not reasons:
        return 'accepted', ['existing_operator_authority_verified'], facts, valid_accepted
    facts['returned_candidates'] = []
    for ref in payload.get('candidate_refs') or []:
        record = runtime.store.get_by_exact_ref(ref, scope=exact, source_id='codex')
        available = record is not None and record.status == 'active'
        facts['returned_candidates'].append({'record_id': ref, 'available': available,
            'record_digest': _stable_digest(record.to_dict()) if record else None})
        if not available:
            reasons.append('returned_candidate_unavailable')
    if not payload.get('candidate_refs'):
        reasons.append('no_positive_label_evidence')
    if accepted and not valid_accepted:
        reasons.append('existing_operator_authority_invalid')
    if reasons:
        return 'evidence_insufficient', sorted(set(reasons)), facts, []
    return 'pending_independent_review', ['independent_operator_labels_required'], facts, []


def _public(body):
    return {key: body[key] for key in ('record_id', 'pending_record_id', 'disposition',
        'reasons', 'reviewer', 'delegator', 'natural_gold_created', 'accepted_record_ids')}


def review_pending_production_queries(runtime, *, scope, channel, delegation_path, limit=500):
    packet, fingerprint, exact = load_review_delegation(delegation_path, scope=scope, channel=channel)
    keys = _receipt_key_set()
    if keys is None:
        raise ValueError('review_service_attestation_key_unavailable')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('review_limit_invalid')
    # Complete the exact bounded snapshot before the first write. Review and
    # receipts share the store's normal transaction/outbox path.
    def mutation(sqlite):
        rows = runtime.store.sqlite.conn.execute(
            'SELECT record_id,source_id FROM records WHERE source=? AND tenant_id=? AND agent_id=? '
            "AND workspace_id=? AND user_id=? AND status IN ('active','quarantined') "
            'ORDER BY record_id LIMIT ?', (PENDING_SOURCE, *asdict(exact).values(), limit + 1)).fetchall()
        if len(rows) > limit:
            raise ValueError('review_scan_overflow')
        pending_records = []
        for row in rows:
            record = runtime.store.get_by_exact_ref(row['record_id'], scope=exact, source_id='codex')
            if record is None or row['source_id'] != 'codex':
                raise ValueError('review_pending_boundary_invalid')
            pending_records.append(record)
        accepted_by_pending = {}
        accepted_rows = runtime.store.sqlite.conn.execute(
            'SELECT record_id FROM records WHERE source=? AND tenant_id=? AND agent_id=? '
            "AND workspace_id=? AND user_id=? AND source_id='codex' AND status='active' ORDER BY record_id LIMIT 501",
            (ACCEPTED_SOURCE, *asdict(exact).values())).fetchall()
        if len(accepted_rows) > 500:
            raise ValueError('review_accepted_scan_overflow')
        for row in accepted_rows:
            record = runtime.store.get_by_exact_ref(row['record_id'], scope=exact, source_id='codex')
            if record is None:
                raise ValueError('review_accepted_boundary_invalid')
            if record.evidence:
                accepted_by_pending.setdefault(record.evidence[0], []).append(record)
        prepared = []
        for pending in pending_records:
            disposition, reasons, facts, accepted_ids = _assessment(
                runtime, pending, exact, accepted_by_pending.get(pending.record_id, []))
            input_digest = _stable_digest(facts)
            record_id = 'prdr_' + _stable_digest({'schema': SCHEMA, 'pending': pending.record_id,
                'delegation_digest': fingerprint['digest'], 'input_digest': input_digest})[:32]
            body = {'schema': SCHEMA, 'source': SOURCE, 'record_id': record_id, 'scope': asdict(exact),
                'pending_record_id': pending.record_id, 'disposition': disposition, 'reasons': reasons,
                'reviewer': packet['delegate'], 'delegator': packet['delegator'],
                'authority_kind': 'local_user_delegation_service_attestation',
                'authorization_ref': packet['authorization_ref'], 'delegation_packet_evidence': fingerprint,
                'input_digest': input_digest, 'facts': facts, 'accepted_record_ids': accepted_ids,
                'natural_gold_created': False, 'key_id': keys.active_id}
            body['signature'] = _signature(body, keys.active_key)
            record = RecordEnvelope.create(kind='evaluation_packet', title='Delegated Codex recall review',
                summary='Automatic review disposition; positive labels retain independent operator authority.',
                content=body, source=SOURCE, source_id='codex', scope=exact, evidence=[pending.record_id],
                meta={'report_type': 'production_recall_delegated_review', 'pending_record_id': pending.record_id,
                      'disposition': disposition, 'channel': 'codex'})
            record.record_id = record_id
            prepared.append(record)

        inserted, results = [], []
        for record in prepared:
            old = sqlite.get_by_exact_ref(record.record_id, scope=exact, source_id='codex')
            if old is None:
                sqlite.upsert(record, commit=False)
                inserted.append(record)
            results.append(_public(verify_delegated_review(old or record, scope=exact)))
        return (len(inserted), results), inserted, []

    created, results = runtime.store.mutate_records_atomically(mutation)
    return {'ok': True, 'schema': SCHEMA, 'created': created, 'reviewed_count': len(results),
            'dispositions': dict(Counter(x['disposition'] for x in results)), 'reviews': results,
            'natural_gold_created': False}
