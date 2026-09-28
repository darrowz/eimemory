"""Bounded, non-authoritative work items from the actual closure control report.

These are diagnostic instructions, not evidence approval or permission to run
repairs. Input paths identify the report boundary; ordering is review order,
not a claim about which distributed event happened first.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import re
from typing import Any

DOMAINS = ('deployment.runtime', 'storage.integrity', 'code.evolution',
           'memory.governance', 'channel.delivery', 'memory.recall')
_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,180}')
_ACTIONS = {
    'record_not_found': 'locate_exact_scope_record',
    'scope_mismatch': 'regenerate_evidence_in_requested_scope',
    'gate_not_after_current_receipt': 'regenerate_evidence_after_current_receipt',
    'current_deployment_receipt_missing': 'verify_current_deployment_receipt',
    'strict_code_evolution_receipt_required': 'obtain_required_operator_authorization',
    'source_not_authorized_for_domain': 'use_domain_authoritative_evidence',
    'explicit_failure': 'reproduce_and_fix_failed_evidence',
    'exact_current_release_replay_manifests_required': 'verify_exact_replay_cohort',
    'current_release_replay_manifests_incomplete': 'complete_verified_replay_cohort',
    'current_release_recall_replay_incomplete': 'complete_verified_recall_replay',
    'lineage_attestation_mismatch': 'revalidate_lineage_with_original_identity',
    'recall_quality_evidence_incomplete': 'collect_and_review_real_relevance_evidence',
}

def _obj(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _safe(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def lineage_blockers(lineage: Any) -> dict[str, Any]:
    """Read exact domain errors without changing a lineage admission decision."""
    node = _obj(lineage)
    domains = _obj(node.get('domains'))
    items: list[dict] = []
    truncated = False
    def add(domain: str, reference: Any, reason: Any, path: str):
        nonlocal truncated
        if len(items) >= 64:
            truncated = True
            return
        text = str(reason)
        code = _safe(reason, 'lineage_gate_error')
        items.append({
            'domain': domain, 'record_id': _safe(reference, ''),
            'reason_code': code, 'reason_sha256': sha256(text.encode('utf-8', errors='replace')).hexdigest(),
            'report_path': path, 'action': _ACTIONS.get(code, 'inspect_exact_gate_contract'),
            'state': 'open', 'repair_complete': False,
            'verification': ['same_scope_commit_receipt_session', 'authoritative_gate_reverification',
                             'fresh_current_release_lineage', 'no_gate_weakening'],
        })
    top = node.get('error') or node.get('reason')
    if top and node.get('ok') is not True:
        add('', '', top, '$.release_lineage.error')
    for domain in DOMAINS:
        if domain not in domains:
            continue
        state = domains[domain]
        path = '$.release_lineage.domains.' + domain
        if not isinstance(state, Mapping):
            add(domain, '', 'lineage_domain_report_invalid', path)
            continue
        errors = state.get('gate_errors')
        if errors is not None and not isinstance(errors, Mapping):
            add(domain, '', 'lineage_gate_errors_invalid', path+'.gate_errors')
        elif errors:
            # Mapping iteration is bounded before any sorting/stringification.
            for index, (reference, reason) in enumerate(errors.items()):
                if index >= 64:
                    truncated = True
                    break
                add(domain, reference, reason, path+'.gate_errors')
        elif state.get('mode') == 'changed_unverified':
            add(domain, '', 'lineage_domain_evidence_missing', path+'.mode')
    unknown_paths = node.get('unknown_production_paths')
    if unknown_paths:
        add('', '', 'unknown_production_paths', '$.release_lineage.unknown_production_paths')
    if node.get('compatible') is False and not items:
        add('', '', 'lineage_failure_detail_missing', '$.release_lineage.compatible')
    return {'schema': 'release_lineage.blockers.v1', 'items': items,
            'truncated': truncated, 'ordering': 'review_order_not_event_time',
            'diagnostic_only': True, 'repair_complete': False}


def closure_blockers(report: Any) -> dict[str, Any]:
    node = _obj(report)
    lineage = node.get('release_lineage')
    if not isinstance(lineage, Mapping) or not lineage:
        lineage = _obj(node.get('closure_rehearsal')).get('release_lineage')
    result = lineage_blockers(lineage)
    recall = _obj(node.get('production_recall_gate'))
    quality = _obj(recall.get('quality_gate')) or _obj(node.get('recall_quality_gate'))
    reason = quality.get('blocked_reason') or recall.get('blocked_reason') or recall.get('reason')
    assessed = bool(quality)
    quality_ok = bool(quality.get('ok') is True and quality.get('vacuous') is not True
                      and not any(quality.get(key) for key in ('error', 'errors', 'blocking_metrics')))
    result['recall_evidence'] = {
        # Absence is unknown, not a failed assessment or an invented success.
        'quality_accepted': quality_ok if assessed else None,
        'quality_assessment_status': ('accepted' if quality_ok else 'not_accepted') if assessed else 'not_reported',
        'production_accepted': recall.get('ok') is True and recall.get('status') == 'accepted',
        'reason_code': _safe(reason, 'recall_evidence_not_established'),
        'action': _ACTIONS.get(str(reason), 'inspect_recall_evidence_contract'),
        'sample_count': recall.get('sample_count') if type(recall.get('sample_count')) is int and recall['sample_count'] >= 0 else None,
        'repair_complete': False,
    }
    result['scope'] = {key: _obj(node.get('scope')).get(key, '')
                       for key in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')}
    return result
