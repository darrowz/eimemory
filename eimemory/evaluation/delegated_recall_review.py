"""Exact user-delegated Codex review with separately attested AI label authority."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
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
            or packet['delegate'] != 'codex' or packet['actions'] not in (['review_pending'], ['review_pending', 'accept_positive_labels'])
            or not isinstance(authorization, dict)
            or set(authorization) not in ({'kind', 'session_id', 'message_digest'},
                {'kind', 'session_id', 'message_digest', 'source_message_id', 'source_store'})
            or authorization['kind'] != 'user_instruction'
            or not isinstance(authorization['session_id'], str)
            or not 1 <= len(authorization['session_id']) <= 100
            or re.fullmatch('[a-f0-9]{64}', str(authorization['message_digest'])) is None):
        raise ValueError('review_delegation_authority_invalid')
    if 'source_message_id' in authorization and (type(authorization['source_message_id']) is not int
            or authorization['source_message_id'] <= 0 or authorization['source_store'] != 'hermes_user_history'):
        raise ValueError('review_delegation_authorization_locator_invalid')
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
            or body.get('reviewer') != 'codex' or type(body.get('natural_gold_created')) is not bool
            or body.get('disposition') not in {'maintenance', 'rejected', 'evidence_insufficient',
                                              'pending_independent_review', 'accepted'}
            or (body.get('natural_gold_created') and (body.get('disposition') != 'accepted' or not body.get('accepted_record_ids')))
            or record.evidence != [body.get('pending_record_id')]
            or record.meta.get('report_type') != 'production_recall_delegated_review'):
        raise ValueError('review_receipt_identity_invalid')
    return record.content


def _assessment(runtime, pending, exact, accepted):
    payload = pending.content
    capture_ref = str(payload.get('capture_ref') or '')
    decision = runtime.store.sqlite.conn.execute(
        'SELECT acceptance_generated FROM proactive_decisions WHERE decision_id=? '
        'AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? '
        "AND channel='codex' AND json_valid(source_ids_json) "
        "AND json_array_length(source_ids_json)=1 AND json_extract(source_ids_json,'$[0]')='codex'",
        (capture_ref, *asdict(exact).values())).fetchone()
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
    def snapshot():
        rows = runtime.store.sqlite.conn.execute(
            'SELECT record_id,source_id FROM records WHERE source=? AND tenant_id=? AND agent_id=? '
            "AND workspace_id=? AND user_id=? AND source_id='codex' AND status IN ('active','quarantined') "
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
        return [(pending, _assessment(runtime, pending, exact,
                    accepted_by_pending.get(pending.record_id, []))) for pending in pending_records]

    with runtime.store._lock:
        snapshots = snapshot()
        rows = runtime.store.sqlite.conn.execute(
            "SELECT record_id FROM records WHERE source=? AND tenant_id=? AND agent_id=? "
            "AND workspace_id=? AND user_id=? AND source_id='codex' AND status='active' "
            "ORDER BY created_at DESC,record_id DESC LIMIT 2000",
            (SOURCE, *asdict(exact).values())).fetchall()
        prior = [runtime.store.get_by_exact_ref(row['record_id'], scope=exact, source_id='codex') for row in rows]
    prepared, reused = [], []
    model_calls = 0
    for pending, assessment in snapshots:
        disposition, reasons, facts, accepted_ids = assessment
        input_digest = _stable_digest(facts)
        previous = None
        retry_parent = None
        for record in prior:
            body = verify_delegated_review(record, scope=exact)
            if (body['pending_record_id'] == pending.record_id
                    and body['delegation_packet_evidence'] == fingerprint
                    and (body['input_digest'] == input_digest or
                         (disposition == 'accepted' and body['accepted_record_ids'] == accepted_ids))):
                retry_at = body.get('retry_after')
                if retry_at and datetime.now(timezone.utc) >= datetime.fromisoformat(retry_at):
                    retry_parent = body['record_id']
                else:
                    previous = body
                break
        if previous:
            reused.append(_public(previous))
            continue
        created_gold = False
        semantic = None
        retry_after = None
        if disposition == 'pending_independent_review' and 'accept_positive_labels' in packet['actions']:
            from .delegated_label_authority import semantic_review
            original = load_query_input(runtime, decision_id=pending.content['capture_ref'],
                scope=exact, channel='codex', source_id='codex')
            candidates = [runtime.store.get_by_exact_ref(ref, scope=exact, source_id='codex')
                          for ref in pending.content['candidate_refs']]
            try:
                if model_calls >= 5:
                    raise ValueError('semantic_review_batch_limit')
                model_calls += 1
                semantic = semantic_review(original, candidates)
                if semantic['labels']:
                    created_gold = True
                    disposition, reasons = 'accepted', ['delegated_semantic_labels_verified']
                else:
                    disposition, reasons = 'rejected', ['semantic_answer_not_supported']
            except Exception:
                disposition, reasons = 'evidence_insufficient', ['semantic_review_unavailable_or_invalid']
                retry_after = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        record_id = 'prdr_' + _stable_digest({'schema': SCHEMA, 'pending': pending.record_id,
            'delegation_digest': fingerprint['digest'], 'input_digest': input_digest, 'retry_parent':retry_parent})[:32]
        body = {'schema': SCHEMA, 'source': SOURCE, 'record_id': record_id, 'scope': asdict(exact),
            'pending_record_id': pending.record_id, 'disposition': disposition, 'reasons': reasons,
            'reviewer': packet['delegate'], 'delegator': packet['delegator'],
            'authority_kind': 'local_user_delegation_service_attestation',
            'authorization_ref': packet['authorization_ref'], 'delegation_packet_evidence': fingerprint,
            'input_digest': input_digest, 'facts': facts, 'accepted_record_ids': accepted_ids,
            'natural_gold_created': created_gold, 'key_id': keys.active_id}
        if retry_after:
            body['retry_after'] = retry_after
        if semantic:
            body['semantic_review'] = semantic
        prepared.append((pending, body, semantic if created_gold else None))

    def mutation(sqlite):
        # The model runs outside the write lock. Recheck the entire evidence
        # snapshot and grant immediately before an atomic labels+acceptance+receipt write.
        current_packet, current_fingerprint, _ = load_review_delegation(delegation_path, scope=scope, channel=channel)
        if current_packet != packet or current_fingerprint != fingerprint:
            raise ValueError('review_delegation_changed')
        current = {p.record_id: _stable_digest(a[2]) for p, a in snapshot()}
        for pending, assessment in snapshots:
            if current.get(pending.record_id) != _stable_digest(assessment[2]):
                raise ValueError('review_evidence_changed')
        inserted, results = [], list(reused)
        for pending, body, semantic in prepared:
            if semantic:
                from .delegated_label_authority import sign, SCHEMA as LABEL_SCHEMA
                from .production_query_dataset import accept_pending_production_query, LABEL_EVIDENCE_SOURCE
                labels = {}
                for selection in semantic['labels']:
                    ref = selection['record_ref']
                    candidate = sqlite.get_by_exact_ref(ref, scope=exact, source_id='codex')
                    identity = {'pending_record_id':pending.record_id, 'record_ref':ref,
                                'grade':selection['grade'], 'labeler':'delegated_ai'}
                    authority = sign({'schema': LABEL_SCHEMA, 'scope':asdict(exact),
                        'delegation':packet, 'delegation_packet_evidence':fingerprint,
                        'reviewer':'codex', 'model_id':semantic['model_id'], 'reviewed_at':semantic['reviewed_at'],
                        'label':identity, 'input_digest':body['facts']['input_digest'],
                        'candidate_digest':_stable_digest(candidate.to_dict()),
                        'query_features_digest':_stable_digest(semantic['query_features']),
                        'quote_digest':sha256(selection['quote'].encode()).hexdigest(), 'reason':selection['reason']})
                    evidence = RecordEnvelope.create(kind='evaluation_packet', title='Delegated AI relevance label',
                        summary='User-delegated AI review; service attestation.', source=LABEL_EVIDENCE_SOURCE,
                        source_id='codex', scope=exact, evidence=[pending.record_id,ref],
                        content={**identity, 'evidence_class':'delegated_ai_relevance_label',
                            'delegation_packet_evidence':fingerprint, 'delegated_authority':authority},
                        meta={'report_type':'production_recall_label_evidence', 'authoritative':True})
                    evidence.record_id = 'prdl_' + _stable_digest(authority)[:32]
                    labels[ref] = evidence
                records = accept_pending_production_query(runtime, pending_record_id=pending.record_id,
                    query_features=semantic['query_features'], labels=[{'record_ref':x['record_ref'], 'grade':x['grade']}
                        for x in semantic['labels']], labeler='delegated_ai', operator_scope=exact,
                    label_packet_evidence=fingerprint, _delegated_labels=labels, _prepare_only=True)
                body['accepted_record_ids'] = [records[-1].record_id]
                for record in records:
                    sqlite.upsert(record, commit=False)
                    inserted.append(record)
            body['signature'] = _signature(body, keys.active_key)
            record = RecordEnvelope.create(kind='evaluation_packet', title='Delegated Codex recall review',
                summary='Automatic review under exact user delegation.', content=body, source=SOURCE,
                source_id='codex', scope=exact, evidence=[pending.record_id],
                meta={'report_type':'production_recall_delegated_review', 'pending_record_id':pending.record_id,
                      'disposition':body['disposition'], 'channel':'codex'})
            record.record_id = body['record_id']
            old = sqlite.get_by_exact_ref(record.record_id, scope=exact, source_id='codex')
            if old is None:
                sqlite.upsert(record, commit=False)
                inserted.append(record)
            results.append(_public(verify_delegated_review(old or record, scope=exact)))
        return (sum(r.source == SOURCE for r in inserted), sum(r.source == ACCEPTED_SOURCE for r in inserted), results), inserted, []

    created, new_accepted_count, results = runtime.store.mutate_records_atomically(mutation)
    return {'ok': True, 'schema': SCHEMA, 'created': created, 'reviewed_count': len(results),
            'dispositions': dict(Counter(x['disposition'] for x in results)), 'reviews': results,
            'natural_gold_created': new_accepted_count > 0, 'new_accepted_count':new_accepted_count, 'model_calls':model_calls}


def collect_and_review_configured(runtime):
    """Existing L1 worker trigger. Unconfigured users retain the existing workflow."""
    import os
    path = os.environ.get('EIMEMORY_CODEX_REVIEW_DELEGATION', '')
    if not path:
        return {'status': 'not_configured'}
    raw, _ = load_json_dataset_with_evidence(path)
    scope = ScopeRef.from_dict(raw.get('scope') or {})
    load_review_delegation(path, scope=scope, channel='codex')
    from .production_query_dataset import collect_pending_production_queries
    collected = collect_pending_production_queries(runtime, scope=scope, channel='codex', source_id='codex', limit=80)
    reviewed = review_pending_production_queries(runtime, scope=scope, channel='codex', delegation_path=path)
    return {**reviewed, 'collection':collected}
