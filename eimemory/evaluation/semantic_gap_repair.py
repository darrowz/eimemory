"""One historical semantic incident -> attested current maintenance repair.

This is neither natural-sample production nor label authority. Old reports,
queries and renders retain their original identities, including renamed agents.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef
from eimemory.evaluation import explicit_recall as explicit
from eimemory.evaluation import semantic_relevance_monitor as semantic
from eimemory.governance.release.evidence_contract import current_release_identity, release_identity_payload

CAPTURE_SOURCE = 'eimemory.semantic_relevance_monitor.replay_capture'
REVIEW_SOURCE = 'eimemory.semantic_relevance_monitor.repair_review'
SCHEMA = 'semantic_gap_repair.v1'


def _exact(scope):
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _current(runtime, scope):
    identity = current_release_identity(runtime, scope)
    if identity is None or not identity.complete:
        raise ValueError('semantic_repair_current_release_unavailable')
    return release_identity_payload(identity)


def _lineage(runtime, gap_id, scope, *, require_current=False):
    prepared, inputs, decision = semantic._semantic_gap_replay_input(
        runtime, gap_id=gap_id, scope=scope, require_current=require_current)
    context = semantic._semantic_gap_context(inputs, decision)
    return prepared, inputs, decision, context


def _actual_result(runtime, bundle, scope, source_id, channel, limit):
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    full = [*bundle.items, *bundle.rules, *bundle.reflections]
    if any(record.scope != scope or record.source_id != source_id for record in full):
        raise ValueError('semantic_repair_result_boundary_invalid')
    assembled = AgentRuntimeMemoryService._assemble_recall_bundle(bundle, limit=limit)
    if assembled.get('evidence'):
        raise ValueError('semantic_repair_legacy_evidence_unverifiable')
    assembled.pop('evidence', None)
    references = {}
    for section in ('items', 'persona', 'rules', 'reflections'):
        for item in assembled.get(section, []):
            matches = {explicit._digest(record.to_dict()): record for record in full
                       if record.record_id == item.get('record_id') and record.source_id == item.get('source_id')}
            if len(matches) != 1:
                raise ValueError('semantic_repair_result_identity_invalid')
            record = next(iter(matches.values()))
            ref = explicit._reference(runtime, record, scope)
            item['scope'] = ref['scope']
            references[explicit._digest(ref)] = ref
    result = dict(ok=True, channel=channel, scope=asdict(scope), bundle=assembled)
    return result, list(references.values())


def _replay_retrieval_state(bundle):
    """Keep unknown counters unknown under the current native sparse contract."""
    explanation = getattr(bundle, 'explanation', {}) or {}
    selector = explanation.get('relevance_selector')
    selector = selector if isinstance(selector, dict) else {}
    engine = explanation.get('engine_diagnostics')
    engine = engine if isinstance(engine, dict) else {}
    source = engine.get('source_budget_exhausted')
    drops = engine.get('drops')
    recall = None
    if isinstance(drops, dict):
        if 'recall_budget_exhausted' in drops:
            recall = drops['recall_budget_exhausted']
        elif len(drops) < 8:
            # Native engine emits a sparse Counter truncated to eight entries.
            # Only an untruncated absent key proves a genuine zero.
            recall = 0
    return dict(collection_complete=selector.get('collection_complete')
                if type(selector.get('collection_complete')) is bool else None,
                status=selector.get('status') if selector.get('status') in
                    ('evidence_found', 'no_evidence', 'unavailable', 'ambiguous', 'degraded') else 'unknown',
                source_budget_exhausted=source if type(source) is int and source >= 0 else None,
                recall_budget_exhausted=recall if type(recall) is int and recall >= 0 else None)


def capture_semantic_gap_replay(runtime, *, gap_id, scope, attempt_id='', _allow_recall=True, _on_recall=None):
    """Run one deliberate faithful engine rerun, retaining an attested packet.

    An operator invokes this separately; nightly only reviews existing packets.
    No new proactive decision, natural sample, human gold, or host message exists.
    """
    from eimemory.evaluation.explicit_input_vault import capture_explicit_input
    exact = _exact(scope)
    prepared, inputs, decision, context = _lineage(runtime, gap_id, exact, require_current=True)
    if (not isinstance(attempt_id, str) or len(attempt_id) > 128
            or explicit._receipt_key_set() is None
            or os.environ.get('EIMEMORY_CAPTURE_ORIGINAL_QUERY') != '1'):
        raise ValueError('semantic_repair_capture_unavailable')
    identity = dict(gap_id=gap_id, original_input_digest=inputs['input_digest'],
                    original_context_digest=explicit._digest(context),
                    release_identity=prepared['current_release_identity'], attempt_id=attempt_id)
    packet_id = 'srpc_' + explicit._digest(identity)[:32]
    prior = runtime.store.get_by_exact_ref(packet_id, scope=exact, source_id='default')
    if prior is not None:
        _load_replay(runtime, packet_id, exact, require_complete=False)
        return dict(status='captured', packet_ref=packet_id, reused=True,
                    natural_benchmark_eligible=False, passing_resolution_created=False)
    if not _allow_recall:
        raise ValueError('semantic_repair_budget_deferred')
    retained = runtime.store.pin_proactive_decisions([decision['decision_id']], reason='pending_case')
    if retained.get('decision_missing_count', 0):
        raise ValueError('semantic_repair_decision_unavailable')
    if _on_recall is not None:
        _on_recall()
    # Reuse the exact saved engine input/context, not the explicit wrapper,
    # which deliberately strips caller task_context. Only the agent rename is
    # mapped in the independent scope argument, never in original signed data.
    bundle = runtime.memory.recall(query=inputs['effective_query'], scope=asdict(exact),
                                   task_context=deepcopy(context), limit=inputs['limit'])
    result, references = _actual_result(runtime, bundle, exact, prepared['source_id'],
                                        prepared['channel'], inputs['limit'])
    state = _replay_retrieval_state(bundle)
    status = (getattr(bundle, 'explanation', {}) or {}).get('retrieval_status')
    state['retrieval_status'] = status if status in {'evidence_found', 'no_evidence', 'degraded', 'unavailable', 'ambiguous'} else 'unknown'
    state['final_output_status'] = result['bundle'].get('retrieval_status', 'unknown')
    state['final_admission_status'] = (result['bundle'].get('recall_diagnostics') or {}).get('admission_status', 'unknown')
    if _current(runtime, exact) != prepared['current_release_identity']:
        raise ValueError('semantic_repair_release_changed')
    request = dict(channel=prepared['channel'], scope=asdict(exact), source_ids=[prepared['source_id']],
                   query_digest=sha256(inputs['effective_query'].encode()).hexdigest(),
                   task_type_digest=sha256(decision['task_type'].encode()).hexdigest(),
                   limit=inputs['limit'], acceptance_generated=True, capture_mode='private',
                   task_context_digest=explicit._digest(context))
    def persist(sqlite):
        existing = sqlite.get_by_exact_ref(packet_id, scope=exact, source_id='default')
        if existing is not None:
            return (existing, False), [], []
        # The existing vault helper joins this transaction instead of committing
        # first. A signed-packet failure rolls back its private input too.
        fresh, _private, _decision, _context = _lineage(runtime, gap_id, exact, require_current=True)
        if fresh != prepared or _current(runtime, exact) != prepared['current_release_identity']:
            raise ValueError('semantic_repair_lineage_changed')
        commitment = capture_explicit_input(runtime, capture_id=packet_id, scope=exact,
            request=request, query=inputs['effective_query'], result=result)
        if commitment.get('status') != 'captured':
            raise ValueError('semantic_repair_private_result_unavailable')
        body = dict(schema=SCHEMA, report_type='semantic_gap_replay_capture', created_at=now_iso(),
                    lineage=prepared, request=request, request_digest=explicit._digest(request),
                    original_context_digest=explicit._digest(inputs['task_context']),
                    replay_context_digest=explicit._digest(context), result=explicit._private_result(result),
                    actual_result_digest=explicit._digest(result), capture_input=commitment,
                    references=references, retrieval_state=state, acceptance_generated=True,
                    natural_benchmark_eligible=False, memory_contribution_certified=False,
                    release_identity=prepared['current_release_identity'],
                    release_reference=explicit._release_reference(runtime, prepared['current_release_identity'],
                        channel=prepared['channel'], scope=exact))
        packet = explicit._record(CAPTURE_SOURCE, packet_id, exact, body)
        packet.meta['report_type'] = 'semantic_gap_replay_capture'
        packet.meta['semantic_repair_gap_ref'] = gap_id
        packet.meta['semantic_repair_release_digest'] = explicit._digest(prepared['current_release_identity'])
        sqlite.upsert(packet, commit=False)
        return (packet, True), [packet], []

    stored, inserted = runtime.store.mutate_records_atomically(persist)
    _load_replay(runtime, stored.record_id, exact, require_complete=False)
    return dict(status='captured', packet_ref=stored.record_id, reused=not inserted,
                retrieval_state=state, natural_benchmark_eligible=False, passing_resolution_created=False)


def _load_replay(runtime, packet_ref, scope, *, require_complete=True):
    """Verify HMAC, full old/new authority and completeness before any judge."""
    from eimemory.evaluation.explicit_input_vault import load_explicit_input
    packet = runtime.store.get_by_exact_ref(packet_ref, scope=scope, source_id='default')
    if packet is None:
        raise ValueError('semantic_repair_packet_unavailable')
    body = explicit._verify(packet, CAPTURE_SOURCE, scope)
    if (body.get('schema') != SCHEMA or body.get('report_type') != 'semantic_gap_replay_capture'
            or body.get('acceptance_generated') is not True
            or body.get('natural_benchmark_eligible') is not False
            or body.get('memory_contribution_certified') is not False):
        raise ValueError('semantic_repair_packet_invalid')
    prepared, inputs, decision, context = _lineage(runtime, body['lineage']['gap_id'], scope)
    if body['lineage'] != prepared or body.get('release_identity') != _current(runtime, scope):
        raise ValueError('semantic_repair_lineage_changed')
    request = body['request']
    expected = dict(channel=prepared['channel'], scope=asdict(scope), source_ids=[prepared['source_id']],
                    query_digest=sha256(inputs['effective_query'].encode()).hexdigest(),
                    task_type_digest=sha256(decision['task_type'].encode()).hexdigest(), limit=inputs['limit'],
                    acceptance_generated=True, capture_mode='private', task_context_digest=explicit._digest(context))
    if (request != expected or body.get('request_digest') != explicit._digest(request)
            or body.get('original_context_digest') != explicit._digest(inputs['task_context'])
            or body.get('replay_context_digest') != explicit._digest(context)
            or body.get('release_reference') != explicit._release_reference(runtime, body['release_identity'],
                channel=prepared['channel'], scope=scope)):
        raise ValueError('semantic_repair_replay_input_mismatch')
    private = load_explicit_input(runtime, capture=packet)
    result = private['result']
    if private['query'] != inputs['effective_query'] or result.get('ok') is not True:
        raise ValueError('semantic_repair_replay_input_mismatch')
    refs = body.get('references')
    if not isinstance(refs, list) or len(refs) > 64:
        raise ValueError('semantic_repair_references_invalid')
    seen = set()
    for ref in refs:
        if ref.get('scope') != asdict(scope) or ref.get('source_id') != prepared['source_id']:
            raise ValueError('semantic_repair_result_boundary_invalid')
        record = runtime.store.get_by_exact_ref(ref['record_ref'], scope=scope, source_id=prepared['source_id'])
        if record is None or explicit._reference(runtime, record, scope) != ref:
            raise ValueError('semantic_repair_result_authority_changed')
        key = explicit._digest(ref)
        if key in seen:
            raise ValueError('semantic_repair_references_invalid')
        seen.add(key)
    items = semantic._explicit_result_items(body, result, scope, prepared['channel'])
    state = body.get('retrieval_state') or {}
    final = result.get('bundle') or {}
    diagnostics = final.get('recall_diagnostics') or {}
    if (state.get('final_output_status') != final.get('retrieval_status', 'unknown')
            or state.get('final_admission_status') != diagnostics.get('admission_status', 'unknown')):
        raise ValueError('semantic_repair_final_state_mismatch')
    if require_complete and (not items or state.get('collection_complete') is not True
            or state.get('retrieval_status') != 'evidence_found'
            or state.get('final_output_status') != 'evidence_found'
            or state.get('final_admission_status') != 'evidence_found'
            or diagnostics.get('source_budget_exhausted', 0) != 0
            or (diagnostics.get('engine_drops') or {}).get('recall_budget_exhausted', 0) != 0
            or state.get('status') != 'evidence_found'
            or type(state.get('source_budget_exhausted')) is not int
            or type(state.get('recall_budget_exhausted')) is not int
            or state['source_budget_exhausted'] != 0 or state['recall_budget_exhausted'] != 0):
        raise ValueError('semantic_repair_retrieval_not_complete')
    return packet, prepared, inputs, items


def _review_identity(packet):
    return explicit._digest(dict(packet_ref=packet.record_id, packet_digest=explicit._digest(packet.content)))


def verify_repair_review(runtime, *, review_ref, scope):
    """Revalidation used by the native governance writer, never caller booleans."""
    exact = _exact(scope)
    review = runtime.store.get_by_exact_ref(review_ref, scope=exact, source_id='default')
    if review is None:
        raise ValueError('semantic_repair_review_unavailable')
    body = explicit._verify(review, REVIEW_SOURCE, exact)
    packet, prepared, _inputs, items = _load_replay(runtime, body['packet_ref'], exact)
    if (body.get('schema') != SCHEMA or body.get('review_identity') != _review_identity(packet)
            or body.get('packet_digest') != explicit._digest(packet.content)
            or body.get('lineage') != prepared or body.get('item_count') != len(items)
            or body.get('natural_benchmark_eligible') is not False
            or body.get('memory_contribution_certified') is not False):
        raise ValueError('semantic_repair_review_identity_invalid')
    parsed = semantic._parse_result(json.dumps({k: body.get(k) for k in
        ('relevance', 'off_topic', 'duplicates', 'unanswered')}), len(items))
    if any(body.get(k) != value for k, value in parsed.items()):
        raise ValueError('semantic_repair_verdict_invalid')
    if (parsed['reason'] != 'evaluated' or parsed['verdict'] != 'relevant'
            or parsed['off_topic'] is not False or parsed['unanswered'] is not False):
        raise ValueError('semantic_repair_not_passed')
    return review, packet, prepared


def review_semantic_gap_replay(runtime, *, packet_ref, scope, allow_model=True, _on_provider_call=None):
    """Independently judge one verified actual result; never fabricate a pass."""
    exact = _exact(scope)
    packet, prepared, inputs, items = _load_replay(runtime, packet_ref, exact)
    identity = _review_identity(packet)
    candidates = runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=exact,
        meta_key='semantic_repair_identity', meta_value=identity, limit=32)
    for existing in candidates:
        if existing.source != REVIEW_SOURCE:
            continue
        try:
            body = explicit._verify(existing, REVIEW_SOURCE, exact)
            if body.get('review_identity') != identity or body.get('packet_digest') != explicit._digest(packet.content):
                continue
            parsed = semantic._parse_result(json.dumps({k: body.get(k) for k in
                ('relevance', 'off_topic', 'duplicates', 'unanswered')}), len(items))
            evaluated = parsed['reason'] == 'evaluated' and all(body.get(k) == value for k, value in parsed.items())
            unavailable = (body.get('reason') in {'tool_free_transport_unavailable', 'completion_unavailable', 'malformed_verdict'}
                and body.get('verdict') == 'unknown' and body.get('relevance') == []
                and body.get('off_topic') is False and body.get('duplicates') is False
                and body.get('unanswered') == 'unknown')
            if not evaluated and not unavailable:
                continue
            return dict(review_ref=existing.record_id, verdict=body['verdict'],
                        provider_calls=0, reused=True, natural_benchmark_eligible=False)
        except (ValueError, KeyError, TypeError):
            continue
    if not allow_model:
        return dict(review_ref=None, verdict='deferred', provider_calls=0, reused=False,
                    natural_benchmark_eligible=False)
    if _on_provider_call is not None:
        _on_provider_call()
    result = semantic._evaluate(inputs['query'], items)
    # Revocation, current release changes, and record edits during judgment
    # invalidate this attempted repair instead of receiving passing credit.
    _load_replay(runtime, packet_ref, exact)
    body = dict(schema=SCHEMA, packet_ref=packet_ref, packet_digest=explicit._digest(packet.content),
                lineage=prepared, review_identity=identity, item_count=len(items), **result,
                natural_benchmark_eligible=False, memory_contribution_certified=False)
    record = explicit._record(REVIEW_SOURCE, 'srpr_' + explicit._digest(body)[:32], exact, body)
    record.meta['semantic_repair_identity'] = identity
    record.meta['semantic_repair_packet_ref'] = packet_ref
    record.meta['semantic_repair_verdict'] = result['verdict']
    stored, inserted = explicit._insert_once(runtime, record)
    explicit._verify(stored, REVIEW_SOURCE, exact)
    return dict(review_ref=stored.record_id, verdict=result['verdict'], provider_calls=1,
                reused=not inserted, natural_benchmark_eligible=False)


def verified_repair_finding(runtime, *, review_ref, scope):
    from eimemory.governance.learning.quality_gap_intake import _quality_finding
    review, packet, prepared = verify_repair_review(runtime, review_ref=review_ref, scope=scope)
    exact = _exact(scope)
    gap = runtime.store.get_by_id(prepared['gap_id'], scope=exact)
    source_name = gap.content['source_report']['name']
    finding = _quality_finding(source_name, dict(target_capability='memory.recall', report_type=SCHEMA,
        record_id=review.record_id, sample_count=1, quality_gate={'ok': True, 'blocking_metrics': {}}))
    if finding['semantic_key'] != gap.content['semantic_key']:
        raise ValueError('semantic_repair_semantic_key_mismatch')
    finding['observation'] = dict(scope=asdict(exact))
    finding['_semantic_repair'] = dict(review_ref=review.record_id, packet_ref=packet.record_id,
        expected_gap_id=gap.record_id, review_digest=explicit._digest(review.content),
        packet_digest=explicit._digest(packet.content))
    return finding


def _audit_unverifiable(runtime, *, scope, reason, gap=None, packet=None):
    body = dict(schema=SCHEMA, status='unverifiable', reason=reason,
                release_identity=_current(runtime, scope),
                gap_ref=gap.record_id if gap is not None else '',
                gap_digest=explicit._digest(gap.to_dict()) if gap is not None else '',
                packet_ref=packet.record_id if packet is not None else '',
                packet_digest=explicit._digest(packet.content) if packet is not None else '',
                natural_benchmark_eligible=False, passing_resolution_created=False)
    record = explicit._record('eimemory.semantic_relevance_monitor.repair_audit',
        'srpa_' + explicit._digest(body)[:32], scope, body)
    record.meta['semantic_repair_gap_ref'] = body['gap_ref']
    record.meta['semantic_repair_packet_ref'] = body['packet_ref']
    record.meta['semantic_repair_release_digest'] = explicit._digest(body['release_identity'])
    explicit._insert_once(runtime, record)


def _incident_cte():
    # Select current semantic tips BEFORE imposing the work limit. Historic
    # failures remain active after append-only resolution and cannot fill it.
    return """WITH owner_records AS (
        SELECT record_id,status,updated_at,
            json_extract(meta_json,'$.semantic_key') AS semantic_key,
            json_extract(meta_json,'$.supersedes_gap_id') AS supersedes_gap_id,
            json_extract(meta_json,'$.resolves_gap_id') AS resolves_gap_id
        FROM records WHERE source=? AND kind='reflection'
            AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?
            AND json_extract(meta_json,'$.semantic_key') LIKE 'quality-gap:recall_semantic:%:memory.recall'
    ), tips AS (
        SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.semantic_key
            ORDER BY r.updated_at DESC,r.record_id DESC) AS current_rank
        FROM owner_records r WHERE NOT EXISTS (
            SELECT 1 FROM owner_records child WHERE child.semantic_key=r.semantic_key
                AND (child.supersedes_gap_id=r.record_id OR child.resolves_gap_id=r.record_id))
    ), current_incidents AS (
        SELECT record_id,updated_at FROM tips WHERE current_rank=1 AND status='active'
    ) """


def collect_semantic_gap_repair_findings(runtime, *, scope, max_new=8):
    """Bounded nightly owner: exact current incidents -> maintenance -> review.

    Every original authority check precedes replay; missing inputs never create
    traffic. Per-case/current-release captures and unknown reviews are reused.
    """
    from eimemory.governance.learning.quality_gap_intake import QUALITY_GAP_SOURCE, _latest_gap
    exact = _exact(scope)
    report = dict(status='observation_only', reviewed_count=0, replay_count=0, provider_calls=0,
                  unresolved_count=0, unverifiable_count=0, deferred_count=0,
                  reason_counts={}, natural_benchmark_eligible=False)
    findings = []
    budget = max(0, min(8, int(max_new)))

    def spent():
        return report['replay_count'] + report['provider_calls']

    def recall_called():
        report['replay_count'] += 1

    def judge_called():
        # Consume at the actual call boundary, even if post-judge revalidation
        # or signing fails. Invalid/revoked packets cannot evade this budget.
        report['provider_calls'] += 1

    def failed(error, gap=None, packet=None):
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith('semantic_repair_') else 'semantic_repair_evidence_invalid'
        if reason == 'semantic_repair_budget_deferred':
            report['deferred_count'] += 1
            return
        report['unverifiable_count'] += 1
        report['reason_counts'][reason] = report['reason_counts'].get(reason, 0) + 1
        if gap is not None or packet is not None:
            try:
                _audit_unverifiable(runtime, gap=gap, packet=packet, scope=exact, reason=reason)
            except (ValueError, KeyError, TypeError):
                pass  # unavailable attestation never turns unknown into pass

    # Independent configured capture policy supplies authority, never records.
    from eimemory.evaluation.query_input_vault import configured_capture_scopes
    allowed = configured_capture_scopes() or []
    if not any(all(entry[k] == v for k, v in asdict(exact).items()) for entry in allowed):
        report['status'] = 'scope_not_authorized'
        return report, findings
    owner_params = (QUALITY_GAP_SOURCE, exact.tenant_id, exact.agent_id, exact.workspace_id, exact.user_id)
    try:
        release_digest = explicit._digest(_current(runtime, exact))
    except ValueError as error:
        failed(error)
        return report, findings
    with runtime.store.locked() as db:
        rows = db.execute(_incident_cte() + """
            SELECT incident.record_id FROM current_incidents incident
            WHERE NOT EXISTS (SELECT 1 FROM records coverage
                WHERE coverage.source IN (?,?) AND coverage.tenant_id=? AND coverage.agent_id=?
                  AND coverage.workspace_id=? AND coverage.user_id=?
                  AND json_extract(coverage.meta_json,'$.semantic_repair_gap_ref')=incident.record_id
                  AND json_extract(coverage.meta_json,'$.semantic_repair_release_digest')=?)
            ORDER BY incident.updated_at ASC,incident.record_id ASC LIMIT 33""",
            (*owner_params, CAPTURE_SOURCE, 'eimemory.semantic_relevance_monitor.repair_audit',
             exact.tenant_id, exact.agent_id, exact.workspace_id, exact.user_id, release_digest)).fetchall()
    if len(rows) > 32:
        report['deferred_count'] += len(rows) - 32
    for row in rows[:32]:
        gap = runtime.store.get_by_id(row['record_id'], scope=exact)
        if gap is None:
            continue
        latest = _latest_gap(runtime, scope=exact, semantic_key=str(gap.meta.get('semantic_key') or ''))
        if latest is None or latest.record_id != gap.record_id:
            continue
        try:
            capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=exact,
                _allow_recall=spent() < budget, _on_recall=recall_called)
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as error:
            failed(error, gap)

    with runtime.store.locked() as db:
        packet_rows = db.execute(_incident_cte() + """
            SELECT packet.record_id FROM records packet
            WHERE packet.source=? AND packet.status='active'
              AND packet.tenant_id=? AND packet.agent_id=? AND packet.workspace_id=? AND packet.user_id=?
              AND json_extract(packet.meta_json,'$.semantic_repair_release_digest')=?
              AND json_extract(packet.meta_json,'$.semantic_repair_gap_ref') IN (SELECT record_id FROM current_incidents)
              AND NOT EXISTS (SELECT 1 FROM records completed
                  WHERE completed.tenant_id=packet.tenant_id AND completed.agent_id=packet.agent_id
                    AND completed.workspace_id=packet.workspace_id AND completed.user_id=packet.user_id
                    AND json_extract(completed.meta_json,'$.semantic_repair_packet_ref')=packet.record_id
                    AND (completed.source=? OR (completed.source=?
                        AND json_extract(completed.meta_json,'$.semantic_repair_verdict') IN ('unknown','off_topic','mixed'))))
            ORDER BY packet.updated_at ASC,packet.record_id ASC LIMIT 64""",
            (*owner_params, CAPTURE_SOURCE, exact.tenant_id, exact.agent_id, exact.workspace_id, exact.user_id,
             release_digest, 'eimemory.semantic_relevance_monitor.repair_audit', REVIEW_SOURCE)).fetchall()
    packets = [runtime.store.get_by_exact_ref(row['record_id'], scope=exact, source_id='default') for row in packet_rows]
    for packet in packets:
        if packet is None or packet.scope != exact or packet.source != CAPTURE_SOURCE:
            continue
        try:
            result = review_semantic_gap_replay(runtime, packet_ref=packet.record_id, scope=exact,
                allow_model=spent() < budget, _on_provider_call=judge_called)
            if result['review_ref'] is None:
                report['deferred_count'] += 1
                continue
            report['reviewed_count'] += 1
            try:
                findings.append(verified_repair_finding(runtime, review_ref=result['review_ref'], scope=exact))
            except ValueError as error:
                if str(error) != 'semantic_repair_not_passed':
                    raise
                report['unresolved_count'] += 1
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as error:
            failed(error, packet=packet)
    return report, findings
