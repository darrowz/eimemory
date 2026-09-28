"""Record release failures without confusing detection, repair and acceptance."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from eimemory.governance.release.closure_verdict import report_digest, summarize_release_closure

if TYPE_CHECKING:
    from eimemory.models.records import ScopeRef

DETECTOR_ID = 'eimemory.release_closure_failure.v1'
INCIDENT_CLASS = 'release.closure_internal_failure'
REPORT_INCIDENT_CLASS = 'release.closure_report_failure'


def detect_release_closure_failure(
    closure_report: Mapping[str, Any], *, detected_at: str,
    execution: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(closure_report, Mapping):
        raise ValueError('closure_report must be a mapping')
    original = dict(closure_report)
    context = dict(execution or {})
    if scope is not None:
        context['expected_scope'] = dict(scope)
    verdict = summarize_release_closure(original, execution=context)
    signals = verdict['failure_signals']
    status = verdict['disposition']
    actionable = status in {'failure_detected', 'diagnosis_required'}
    deployment = original.get('deployment')
    deployment = deployment if isinstance(deployment, Mapping) else {}
    receipt = original.get('deployment_receipt')
    receipt = receipt if isinstance(receipt, Mapping) else {}
    commit = str(context.get('expected_commit') or deployment.get('commit') or '').strip().lower()
    first = (signals['hard_errors'] or signals['diagnosis'] or [{}])[0]
    stage = str(original.get('blocked_stage') or 'report_validation')
    reason = str(first.get('code') or original.get('blocked_reason') or 'closure_diagnosis_required')
    # This route has a different protected plan. Report defects must not be
    # forced to edit the receipt-evidence builder to satisfy an unrelated gate.
    report_failure = bool(verdict['contract_error'] or status == 'diagnosis_required'
                          or any(item['code'].startswith(('closure_', 'report_', 'control_', 'cyclic_'))
                                 or item['code'] == 'release_closure_report_contract_invalid'
                                 for item in signals['hard_errors']))
    incident_class = REPORT_INCIDENT_CLASS if report_failure else INCIDENT_CLASS
    identity = {
        'schema': 'release_closure_incident_identity.v2', 'detector': DETECTOR_ID,
        'release_commit': commit,
        'receipt_id': str(deployment.get('promotion_request_id') or receipt.get('promotion_request_id') or ''),
        'release_session_id': str(receipt.get('release_session_id') or deployment.get('release_session_id') or ''),
        'scope': dict(scope or original.get('scope') or {}),
        'attempt_id': str(context.get('attempt_id') or ''),
        'report_digest': verdict['report_digest'], 'status': status,
        'source_sha256': str(context.get('source_sha256') or ''),
        'hard_errors': signals['hard_errors'], 'diagnosis': signals['diagnosis'],
        'incident_class': incident_class,
    }
    incident = None
    if actionable:
        digest = report_digest(identity)
        requirements = [
            'same_report_fingerprint_reproduction', 'first_failure_boundary_identified',
            'protected_regression_tests_pass', 'evidence_gates_not_weakened',
            'current_release_lineage_compatible', 'fresh_release_bound_revalidation',
            'successful_submission_is_not_incident_resolution',
        ]
        if str(original.get('blocked_reason') or '') == 'code_evolution_gate_evidence_missing':
            requirements += [
                'code_evolution_gate_uses_exact_current_deployment_receipt',
                'deployment_receipt_is_authoritative_input',
                'storage_acceptance_records_are_not_deployment_receipts',
                'deployment_receipt_fallback_is_forbidden', 'missing_deployment_receipt_fails_closed',
            ]
        incident = {
            'incident_id': 'incident-release-closure-' + digest[:24],
            'incident_digest': digest, 'incident_class': incident_class,
            'title': 'Release closure requires bounded diagnosis' if status == 'diagnosis_required'
                     else 'Release closure failure detected',
            'summary': f'Release {commit or "unknown"}: {stage}: {reason}. '
                       'Preserve the original report, identify the first failed boundary, '
                       'and revalidate with fresh release-bound evidence. '
                       'Do not weaken gates or infer technical deployment success from this report. '
                       'The deployment receipt is authoritative; never infer or replace it from live record IDs.',
            'diagnostic_codes': [x['code'] for x in signals['hard_errors'] + signals['diagnosis']] or [reason],
            'acceptance_requirements': requirements,
        }
    return {
        'schema': 'release_closure_failure.v2', 'detector': DETECTOR_ID,
        'detected_at': str(detected_at), 'release_commit': commit,
        'release_version': str(deployment.get('version') or ''),
        'blocked_stage': stage, 'blocked_reason': reason,
        'ok': not actionable, 'status': status, 'actionable': actionable,
        'origin': 'system_detector', 'known_before_detection': False,
        'prior_user_reported': False, 'manual_bootstrap': False,
        'observation_valid': True, 'incident': incident, 'identity': identity,
        'report_digest': verdict['report_digest'], 'validation': verdict,
        'repair_eligible': status == 'failure_detected' and bool(commit),
        'repair_complete': False,
    }


def record_release_closure_failure(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any],
    closure_report: Mapping[str, Any], detected_at: str,
    execution: Mapping[str, Any] | None = None,
    capture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from eimemory.models.records import RecordEnvelope, ScopeRef

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    report = detect_release_closure_failure(closure_report, detected_at=detected_at,
                                           execution=execution, scope=asdict(scope_ref))
    incident = report.get('incident')
    if not isinstance(incident, Mapping):
        return {**report, 'recording_ok': True, 'incident_record_id': '', 'repair_status': 'not_required'}
    digest = incident['incident_digest']
    record = RecordEnvelope.create(
        kind='incident', title=incident['title'], summary=incident['summary'],
        detail='Original report fingerprint and decision are preserved; detection is not resolution.',
        content={**dict(incident), 'detector_report': report, 'capture': dict(capture or {}),
                 'repair_status': 'diagnosis_required' if not report['repair_eligible'] else 'registered',
                 'repair_complete': False},
        tags=['code-evolution', 'release-closure', 'system-detected'],
        source='eimemory.release_closure_failure', scope=scope_ref, status='active',
        provenance={'origin': 'system_detector', 'detector': DETECTOR_ID,
                    'known_before_detection': False, 'prior_user_reported': False},
        meta={'idempotency_key': 'release-closure-failure:' + digest,
              'incident_class': incident['incident_class'], 'incident_digest': digest,
              'report_digest': report['report_digest'], 'observation_valid': True},
    )
    record.record_id = incident['incident_id']
    # append(existing_match=...) performs exact-scope insert-once under the
    # store's own BEGIN IMMEDIATE. No racy list-then-random-ID insertion.
    def same(existing):
        return (existing.scope == scope_ref and existing.kind == 'incident'
                and existing.source == record.source
                and existing.meta.get('incident_digest') == digest
                and existing.content.get('detector_report', {}).get('identity') == report['identity'])
    stored = runtime.store.append(record, existing_match=same)
    if not same(stored):
        raise ValueError('closure_incident_identity_mismatch')
    return {**report, 'recording_ok': True, 'incident_record_id': stored.record_id,
            'repair_status': stored.content.get('repair_status') or 'registered'}


__all__ = ['DETECTOR_ID', 'INCIDENT_CLASS', 'REPORT_INCIDENT_CLASS',
           'detect_release_closure_failure', 'record_release_closure_failure']
