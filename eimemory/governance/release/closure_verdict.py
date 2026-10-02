"""One pure report decision shared by summary and incident recording.

Report validity, release admission and repair disposition are separate claims.
Digests correlate inputs; they are not signatures or evidence authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.governance.release.closure_contracts import (
    acceptance_failure_details, channel_wait_report_ok,
    legacy_release_replay_ok, live_acceptance_report_ok,
)

def _reported_release_summary(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("release closure report must be an object")
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    recall_gate = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    recall_threshold = (
        recall_gate.get("threshold_gate")
        if isinstance(recall_gate.get("threshold_gate"), dict)
        else {}
    )
    if "gate_ok" in recall_gate:
        recall_gate_ok = recall_gate.get("gate_ok") is True
    elif recall_threshold:
        recall_gate_ok = recall_threshold.get("ok") is True
    else:
        recall_gate_ok = recall_gate.get("ok") is True
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    channel = (
        report.get("channel_acceptance")
        if isinstance(report.get("channel_acceptance"), dict)
        else {}
    )
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    rehearsal_complete = rehearsal.get("closure_complete") is True
    rehearsal_accumulating = rehearsal.get("data_accumulating") is True
    closure_complete = report.get("closure_complete") is True
    data_accumulating = report.get("data_accumulating") is True
    if (
        report.get("ok") is True
        and report.get("report_type") == "code_evolution_pre_observation"
        and report.get("status") == "ready_for_observation"
        and not closure_complete
        and not data_accumulating
    ):
        business_closure_outcome = "ready_for_observation"
    elif report.get("ok") is True and closure_complete and not data_accumulating:
        business_closure_outcome = "closure_complete"
    elif report.get("ok") is True and data_accumulating and not closure_complete:
        business_closure_outcome = "data_accumulating"
    elif channel_wait_report_ok(report):
        # This is a bound wait for external evidence, never closure success.
        business_closure_outcome = "data_accumulating"
        data_accumulating = True
    else:
        business_closure_outcome = "failed"
    return {
        "ok": report.get("ok") is True,
        "business_closure_outcome": business_closure_outcome,
        "report_type": str(report.get("report_type") or ""),
        "observation_admission_status": str(report.get("status") or "") if report.get("report_type") == "code_evolution_pre_observation" else "",
        "closure_complete": closure_complete,
        "data_accumulating": data_accumulating,
        "blocked_stage": str(report.get("blocked_stage") or ""),
        "blocked_reason": str(report.get("blocked_reason") or ""),
        "commit": str(deployment.get("commit") or ""),
        "version": str(deployment.get("version") or ""),
        "receipt_id": str(deployment.get("promotion_request_id") or ""),
        "production_recall_gate_ok": recall_gate_ok,
        "production_recall_gate_status": str(
            recall_gate.get("status") or recall_gate.get("gate_status") or ""
        ),
        "production_recall_gate_report_id": str(
            recall_gate.get("report_id")
            or recall_gate.get("record_id")
            or recall_gate.get("persisted_record_id")
            or ""
        ),
        "production_recall_gate_reason": str(
            recall_gate.get("reason") or recall_gate.get("blocked_reason") or ""
        ),
        "replay_ok": replay.get("ok") is True,
        "acceptance_failure": (
            acceptance_failure_details(replay.get("capability_acceptance"))
            if replay.get("ok") is not True and report.get("blocked_stage") == "replay_bootstrap"
            else {}
        ),
        "live_acceptance_ok": live.get("ok") is True,
        "live_pass_count": live.get("pass_count") if type(live.get("pass_count")) is int else 0,
        "live_case_count": live.get("case_count") if type(live.get("case_count")) is int else 0,
        "channel_acceptance_ok": channel.get("ok") is True,
        "channel_acceptance_record_id": str(channel.get("record_id") or ""),
        "rehearsal_ok": rehearsal.get("ok") is True and rehearsal_complete != rehearsal_accumulating,
        "readiness_stage": str(readiness.get("current_stage") or readiness.get("status") or ""),
        "readiness_score": readiness.get("readiness_score"),
    }

def _release_closure_summary_contract_ok(report: object, summary: dict[str, Any]) -> bool:
    if not isinstance(report, dict) or summary.get("ok") is not True:
        return False
    if report.get("report_type") == "code_evolution_pre_observation":
        # The installer invokes this script with -I and system Python. Import
        # only the matching release's dependency-free structural contract.
        from eimemory.governance.release_pre_observation import pre_observation_report_ok

        return pre_observation_report_ok(report)
    if report.get("report_type") != "l5_release_closure":
        return False
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    commit = str(deployment.get("commit") or "").strip().lower()
    version = str(deployment.get("version") or "").strip()
    receipt_id = str(deployment.get("promotion_request_id") or "").strip()
    receipt = report.get("deployment_receipt") if isinstance(report.get("deployment_receipt"), dict) else {}
    session_id = str(receipt.get("release_session_id") or "").strip()
    release_identity = {
        "release_commit": commit,
        "release_version": version,
        "deployment_receipt_id": receipt_id,
        "release_session_id": session_id,
    }
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    channel = (
        report.get("channel_acceptance")
        if isinstance(report.get("channel_acceptance"), dict)
        else {}
    )
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    recall = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    readiness_identity = (
        readiness.get("release_identity") if isinstance(readiness.get("release_identity"), dict) else {}
    )
    live_deployment = live.get("deployment") if isinstance(live.get("deployment"), dict) else {}
    common = bool(
        re.fullmatch(r"[0-9a-f]{40}", commit)
        and receipt_id
        and session_id
        and receipt.get("ok") is True
        and receipt.get("commit") == commit
        and receipt.get("promotion_request_id") == receipt_id
        and receipt.get("release_session_id") == session_id
        and deployment.get("release_path") == receipt.get("release_path")
        and isinstance(report.get("storage_migrations"), dict)
        and report["storage_migrations"].get("ok") is True
        and not str(report.get("blocked_stage") or "")
        and not str(report.get("blocked_reason") or "")
        and replay.get("ok") is True
        and legacy_release_replay_ok(replay)
        and live_acceptance_report_ok(live, receipt=receipt)
        and channel.get("ok") is True
        and channel.get("evidence_class") == "external_channel_receipt"
        and str(channel.get("record_id") or "")
        and _deployment_identity_matches(
            live_deployment,
            commit=commit,
            receipt_id=receipt_id,
        )
        and readiness.get("ok") is True
        and readiness.get("schema_version") == "l5_readiness.v2"
        and _release_authority_matches(readiness_identity, release_identity)
    )
    if not common:
        return False
    complete = report.get("closure_complete") is True
    accumulating = report.get("data_accumulating") is True
    if complete == accumulating:
        return False
    if accumulating:
        pending = (
            report.get("bootstrap_pending_verification")
            if isinstance(report.get("bootstrap_pending_verification"), dict)
            else {}
        )
        recall_pending = recall.get("bootstrap") if isinstance(recall.get("bootstrap"), dict) else {}
        rehearsal_pending = (
            rehearsal.get("bootstrap_pending_verification")
            if isinstance(rehearsal.get("bootstrap_pending_verification"), dict)
            else {}
        )
        pending_record_id = str(pending.get("record_id") or "")
        score = readiness.get("readiness_score")
        return bool(
            recall.get("status") == "data_accumulating"
            and all(
                item.get("ok") is True
                and item.get("status") == "bootstrap_data_pending"
                and str(item.get("record_id") or "") == pending_record_id
                and _release_authority_matches(
                    item.get("release_identity"),
                    release_identity,
                )
                for item in (pending, recall_pending, rehearsal_pending)
            )
            and pending_record_id
            and rehearsal.get("ok") is True
            and rehearsal.get("closure_complete") is False
            and rehearsal.get("data_accumulating") is True
            and readiness.get("current_stage") == "L4.5"
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
            and float(score) == 0.8
        )
    strict = (
        report.get("production_recall_strict_state")
        if isinstance(report.get("production_recall_strict_state"), dict)
        else {}
    )
    score = readiness.get("readiness_score")
    return bool(
        recall.get("ok") is True
        and recall.get("status") == "accepted"
        and strict.get("ok") is True
        and strict.get("status") == "strict_activated"
        and str(strict.get("candidate_commit") or "") == commit
        and rehearsal.get("ok") is True
        and rehearsal.get("closure_complete") is True
        and rehearsal.get("data_accumulating") is False
        and readiness.get("current_stage") == "L5"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 1.0
    )

def _deployment_identity_matches(
    deployment: dict[str, Any],
    *,
    commit: str,
    receipt_id: str,
) -> bool:
    return bool(
        deployment.get("commit") == commit
        and deployment.get("promotion_request_id") == receipt_id
    )

def _release_authority_matches(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = (
        "release_commit",
        "deployment_receipt_id",
        "release_session_id",
    )
    return bool(
        all(str(left.get(key) or "").strip() for key in keys)
        and all(
            str(left.get(key) or "").strip() == str(right.get(key) or "").strip()
            for key in keys
        )
    )

def _exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


WAIT_REASONS = frozenset({
    'current_release_channel_receipt_not_found', 'production_dataset_not_ready',
    'production_recall_dataset_empty', 'production_recall_dataset_unconfigured',
    'eligible_dataset_missing', 'recall_quality_evidence_incomplete',
    'sample_starved_or_unconfigured', 'query_features_low_signal',
    'awaiting_evidence', 'evidence_waiting', 'waiting_for_observation',
    'prompt_safety_not_ready', 'tip_safety_not_ready',
    'evidence_insufficient', 'insufficient_evidence', '证据不足',
})
# These describe an unresolved diagnosis, not a demonstrated sample-only wait.
DIAGNOSIS_REASONS = frozenset({
    'bootstrap_pending_non_recall_l5_evidence_incomplete', 'not_ready',
    'observation_not_valid',
})
POLICY_REASONS = frozenset({'strict_code_evolution_receipt_required', 'finish_closure_first'})
_CONTROL_OBJECTS = frozenset({
    'release_lineage', 'deployment_receipt', 'production_recall_gate',
    'production_recall_strict_state', 'storage_migrations', 'replay_bootstrap',
    'live_acceptance', 'channel_acceptance', 'closure_rehearsal', 'readiness',
    'bootstrap_pending_verification', 'capability_acceptance', 'replay_gate',
    'core_replay_gate', 'quality_gate', 'assessment', 'validation',
    'nightly_diagnostics', 'change_policy', 'failure_recording',
})
_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,180}')

def report_digest(report: Any) -> str:
    """Deterministic JSON fingerprint, never a proof of provenance."""
    return sha256(json.dumps(report, ensure_ascii=True, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _signal(path: str, value: Any, *, fallback: str = 'reported_error') -> dict:
    raw = str(value)
    return {'path': path, 'code': raw if _CODE.fullmatch(raw) else fallback,
            'error_sha256': sha256(raw.encode('utf-8', errors='replace')).hexdigest()}


def failure_signals(report: Any) -> dict:
    """Inspect control envelopes, never arbitrary text, fixtures or sample bodies.

    A hard error wins over waits anywhere in the same control envelope. Depth
    and node bounds fail closed. Unrun stages are not falsely called failures.
    """
    hard, waits, unknown = [], [], []
    count = 0
    seen: set[int] = set()

    signal_seen: set[int] = set()
    signal_budget = 0

    def add(path, value, *, force=False, depth=0):
        nonlocal signal_budget
        signal_budget += 1
        if signal_budget > 2048 or depth > 12:
            if not any(item['code'] == 'diagnostic_budget_exceeded' for item in hard):
                hard.append(_signal('$signals', 'diagnostic_budget_exceeded'))
            return
        if value is None or value is False or value == '':
            return
        if isinstance(value, (Mapping, list, tuple)):
            if id(value) in signal_seen:
                hard.append(_signal(path, 'cyclic_control_report')); return
            signal_seen.add(id(value))
            try:
                iterator = value.items() if isinstance(value, Mapping) else enumerate(value)
                for key, item in iterator:
                    if signal_budget >= 2048:
                        add(path, None, force=True, depth=depth+1); break
                    key = str(key)
                    safe_key = key if _CODE.fullmatch(key) else 'field-' + sha256(key.encode()).hexdigest()[:12]
                    child_path = f'{path}.{safe_key}'
                    if isinstance(item, Mapping) and ('reason' in item or 'code' in item):
                        add(child_path, item.get('reason') or item.get('code'), force=force, depth=depth+1)
                    else:
                        add(child_path, item, force=force, depth=depth+1)
            finally:
                signal_seen.remove(id(value))
            return
        text = str(value).strip()
        bucket = hard if force else (waits if text in WAIT_REASONS or text in POLICY_REASONS
                                     else unknown if text in DIAGNOSIS_REASONS else hard)
        bucket.append(_signal(path, value))

    def visit(node, path, depth):
        nonlocal count
        count += 1
        if count > 2048 or depth > 12:
            hard.append(_signal(path, 'diagnostic_budget_exceeded')); return
        if not isinstance(node, Mapping):
            hard.append(_signal(path, 'control_report_not_object')); return
        if id(node) in seen:
            hard.append(_signal(path, 'cyclic_control_report')); return
        seen.add(id(node))
        try:
            if path.endswith('.failure_recording') and node.get('recording_ok') is False:
                add(path+'.recording_ok', 'incident_recording_failed', force=True)
            if 'ok' in node and type(node['ok']) is not bool:
                hard.append(_signal(path+'.ok', 'non_boolean_ok'))
            if node.get('status') == 'not_run' and node.get('reason') == 'upstream_gate_not_run':
                # Actual error fields still win; only the placeholder reason is ignored.
                placeholder = True
            else:
                placeholder = False
            for key in ('contract_error', 'error', 'errors', 'gate_errors', 'blocking_metrics'):
                if key in node:
                    add(path+'.'+key, node[key], force=key in {'contract_error', 'blocking_metrics'})
            for key in ('blocked_reason', 'blocked_reasons', 'reason', 'missing_evidence', 'gaps'):
                if (key in node and not (placeholder and key == 'reason')
                    and not (key == 'reason' and node.get('ok') is True)):
                    add(path+'.'+key, node[key])
            # A concrete incompatible lineage is not made harmless by an unrelated wait.
            if path.endswith('release_lineage') and node.get('compatible') is False and not placeholder:
                add(path+'.compatible', 'release_lineage_not_compatible')
            if path.endswith('release_lineage') and 'domains' in node:
                domains = node['domains']
                if not isinstance(domains, Mapping):
                    add(path+'.domains', 'lineage_domains_invalid', force=True)
                else:
                    # Domain-specific gate errors are the actionable boundary,
                    # not just the top-level compatible=False summary.
                    for index, (domain, state) in enumerate(domains.items()):
                        if index >= 64:
                            add(path+'.domains', 'diagnostic_budget_exceeded', force=True)
                            break
                        safe_domain = domain if isinstance(domain, str) and _CODE.fullmatch(domain) else 'invalid-domain'
                        location = path+'.domains.'+safe_domain
                        visit(state, location, depth+1)
                        if isinstance(state, Mapping) and state.get('mode') == 'changed_unverified' and not state.get('gate_errors'):
                            add(location+'.mode', 'lineage_domain_evidence_missing', force=True)
            for key in sorted(_CONTROL_OBJECTS):
                if key in node:
                    visit(node[key], path+'.'+key, depth+1)
        finally:
            seen.remove(id(node))

    visit(report, '$', 0)
    def unique(items):
        return [dict(t) for t in sorted({tuple(sorted(x.items())) for x in items})]
    return {'hard_errors': unique(hard), 'waits': unique(waits), 'diagnosis': unique(unknown)}


def _envelope_errors(report: Any) -> list[str]:
    if not isinstance(report, dict): return ['report_not_object']
    errors = []
    if report.get('report_type') not in {'l5_release_closure', 'code_evolution_pre_observation'}:
        errors.append('report_type_invalid')
    for key in ('ok', 'closure_complete', 'data_accumulating'):
        if type(report.get(key)) is not bool: errors.append(key+'_not_boolean')
    if report.get('closure_complete') is True and (
        report.get('ok') is not True or report.get('data_accumulating') is not False
    ): errors.append('contradictory_completion_flags')
    if report.get('ok') is False and not (
        isinstance(report.get('blocked_stage'), str) and report['blocked_stage'].strip()
        and isinstance(report.get('blocked_reason'), str) and report['blocked_reason'].strip()
    ): errors.append('blocked_report_missing_stage_or_reason')
    return errors


_QUALITY_GAP_REASONS = frozenset({
    "bootstrap_pending_non_recall_l5_evidence_incomplete",
    "readiness_not_l5",
    "bootstrap_data_pending_readiness_invalid",
})
_ORDINARY_DENY_STAGES = frozenset({
    "deployment_receipt",
    "replay_bootstrap",
    "live_acceptance",
    "storage_migrations",
    "pending_checkpoint",
    "report_read",
})


def _ordinary_receipt_ok(report: dict[str, Any]) -> bool:
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    receipt = report.get("deployment_receipt") if isinstance(report.get("deployment_receipt"), dict) else {}
    commit = str(deployment.get("commit") or "").strip().lower()
    receipt_id = str(deployment.get("promotion_request_id") or "").strip()
    session_id = str(receipt.get("release_session_id") or "").strip()
    path = deployment.get("release_path")
    return bool(
        re.fullmatch(r"[0-9a-f]{40}", commit)
        and receipt_id
        and session_id
        and path
        and receipt.get("ok") is True
        and str(receipt.get("commit") or "").strip().lower() == commit
        and receipt.get("promotion_request_id") == receipt_id
        and receipt.get("release_session_id") == session_id
        and receipt.get("release_path") == path
    )


def split_release_conclusions(
    report: object,
    *,
    execution: Mapping | None = None,
    hard_errors: list[Mapping] | None = None,
) -> dict[str, str | bool]:
    """Separate ordinary release admission from quality and L5 certification.

    A quality or L5 gap does not deny an ordinary release, but only when the
    deployment receipt identity matches and the closure process did not fail.
    Raw completion flags do not certify quality or L5.
    """
    denied = {
        "ordinary_release_admission": "denied",
        "l5_certification": "not_reported",
        "production_quality": "not_reported",
        "historical_pending_blocks_ordinary_release": False,
    }
    if not isinstance(report, dict):
        return denied
    execution = dict(execution or {})
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    commit = str(deployment.get("commit") or "").strip().lower()
    expected = str(execution.get("expected_commit") or "").strip().lower()
    exit_status = execution.get("closure_exit_status")
    reason = str(report.get("blocked_reason") or "")
    stage = str(report.get("blocked_stage") or "")
    recall = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    readiness_stage = str(readiness.get("current_stage") or readiness.get("status") or "")
    sample_count = recall.get("sample_count")
    threshold = recall.get("threshold_gate") if isinstance(recall.get("threshold_gate"), dict) else {}
    process_failed = exit_status not in (None, 0, 1)
    commit_mismatch = bool(expected and commit and expected != commit)
    contradiction = bool(hard_errors or commit_mismatch or process_failed)
    evidence_ok = type(sample_count) is int and sample_count > 0
    l5_certified = bool(
        not contradiction
        and evidence_ok
        and report.get("ok") is True
        and report.get("closure_complete") is True
        and report.get("data_accumulating") is not True
        and readiness.get("ok") is True
        and readiness_stage == "L5"
        and recall.get("ok") is True
        and recall.get("gate_ok") is not False
        and threshold.get("ok") is not False
        and str(recall.get("status") or "") == "accepted"
    )
    quality_certified = bool(
        not contradiction
        and evidence_ok
        and recall.get("ok") is True
        and recall.get("gate_ok") is not False
        and threshold.get("ok") is not False
        and str(recall.get("status") or "") == "accepted"
    )
    gap = reason in _QUALITY_GAP_REASONS and stage not in _ORDINARY_DENY_STAGES
    if (
        not _ordinary_receipt_ok(report)
        or commit_mismatch
        or process_failed
        or hard_errors
        or stage in _ORDINARY_DENY_STAGES
    ):
        ordinary = "denied"
    elif gap or (
        report.get("ok") is True
        and report.get("closure_complete") is True
        and report.get("data_accumulating") is not True
    ):
        ordinary = "admitted"
    else:
        ordinary = "denied"
    if l5_certified:
        l5 = "certified"
    elif readiness or reason in _QUALITY_GAP_REASONS:
        l5 = "incomplete"
    else:
        l5 = "not_reported"
    if quality_certified:
        quality = "certified"
    elif recall or gap:
        quality = "uncertified"
    else:
        quality = "not_reported"
    return {
        "ordinary_release_admission": ordinary,
        "l5_certification": l5,
        "production_quality": quality,
        "historical_pending_blocks_ordinary_release": False,
    }


def summarize_release_closure(report: object, *, execution: Mapping | None = None) -> dict[str, Any]:
    """Classify a valid failure separately from a malformed success/report.

    `contract_ok` retains its prior admission meaning for existing consumers.
    `report_contract_ok` is format validity. Neither field authorizes repair.
    """
    errors = _envelope_errors(report)
    obj = report if isinstance(report, dict) else {}
    try:
        digest = report_digest(report)
        signals = failure_signals(obj)
    except (ValueError, TypeError, RecursionError, OverflowError):
        digest = ''
        signals = {'hard_errors': [_signal('$', 'report_not_finite_json')], 'waits': [], 'diagnosis': []}
        errors.append('report_not_finite_json')
    try:
        raw = _reported_release_summary(obj)
        success_contract = _release_closure_summary_contract_ok(obj, raw)
        waiting_contract = channel_wait_report_ok(obj)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raw = {'ok': obj.get('ok') is True, 'closure_complete': False,
               'data_accumulating': False, 'business_closure_outcome': 'failed',
               'replay_ok': False, 'live_acceptance_ok': False,
               'blocked_stage': '', 'blocked_reason': ''}
        success_contract = waiting_contract = False
        errors.append('control_contract_evaluation_failed')
    for item in signals['hard_errors']:
        if item['code'] in {'control_report_not_object', 'non_boolean_ok', 'cyclic_control_report',
                            'diagnostic_budget_exceeded', 'release_closure_report_contract_invalid'}:
            errors.append(item['path'] + ':' + item['code'])
    if obj.get('ok') is True and not success_contract:
        errors.append('claimed_success_contract_invalid')
    execution = dict(execution or {})
    commit = str(_object(obj.get('deployment')).get('commit') or '')
    expected = str(execution.get('expected_commit') or '')
    if expected and commit and commit != expected:
        signals['hard_errors'].append(_signal('$.deployment.commit', 'closure_report_release_mismatch'))
    expected_scope = execution.get('expected_scope')
    if expected_scope is not None and 'scope' in obj:
        actual_scope = obj['scope']
        keys = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')
        def normalized_scope(value):
            return {key: value.get(key) or ('default' if key == 'tenant_id' else '') for key in keys}
        if (not isinstance(actual_scope, Mapping) or not isinstance(expected_scope, Mapping)
                or normalized_scope(actual_scope) != normalized_scope(expected_scope)):
            signals['hard_errors'].append(_signal('$.scope', 'closure_report_scope_mismatch'))
    exit_status = execution.get('closure_exit_status')
    if exit_status is not None:
        if type(exit_status) is not int or not 0 <= exit_status <= 255:
            signals['hard_errors'].append(_signal('$execution', 'invalid_closure_exit_status'))
        elif exit_status not in {0, 1} or (exit_status != 0 and obj.get('ok') is True):
            signals['hard_errors'].append(_signal('$execution', 'closure_process_failed'))
    for field in ('blocked_stage', 'blocked_reason', 'production_recall_gate_reason'):
        value = raw.get(field)
        if isinstance(value, str) and value and not _CODE.fullmatch(value):
            raw[field] = 'redacted_diagnostic'
    value = raw.get('readiness_score')
    if value is not None:
        import math
        try:
            if type(value) not in (int, float) or not math.isfinite(value): raw['readiness_score'] = None
        except OverflowError:
            raw['readiness_score'] = None
    if (not errors and success_contract
            and obj.get('report_type') == 'code_evolution_pre_observation'):
        # pre_observation_report_ok requires rehearsal to stop exactly at
        # l5_readiness_not_l5 and validates the pending readiness gaps with
        # _l5_observation_semantics. Those validated pending signals are the
        # observation wait, not failures; any other hard error still vetoes.
        pending, remaining = [], []
        for item in signals['hard_errors']:
            path = str(item.get('path') or '')
            if path.startswith('$.readiness.gaps.') or (
                path.startswith('$.closure_rehearsal.blocked_reasons.')
                and item.get('code') == 'l5_readiness_not_l5'
            ):
                pending.append(item)
            else:
                remaining.append(item)
        signals['hard_errors'] = remaining
        signals['waits'] = [*signals['waits'], *pending]
    if errors:
        signals['hard_errors'].insert(0, _signal('$contract', 'release_closure_report_contract_invalid'))
    hard = signals['hard_errors']
    admitted = bool(not errors and not hard and (success_contract or waiting_contract))
    if hard:
        disposition = 'failure_detected'
    elif admitted and raw.get('closure_complete'):
        disposition = 'complete'
    elif signals['diagnosis']:
        disposition = 'diagnosis_required'
    elif signals['waits']:
        disposition = 'evidence_waiting'
    elif admitted:
        disposition = 'non_actionable'
    else:
        disposition = 'diagnosis_required'
    certified = bool(admitted and raw.get('closure_complete') and not raw.get('data_accumulating'))
    reason = str(obj.get('blocked_reason') or '')
    result = {
        **raw, 'summary_schema_version': 'release_closure_summary.v3',
        'validation_scope': 'structural_report_contract_not_independent_attestation',
        'report_digest': digest, 'reported_ok': raw['ok'],
        'reported_closure_complete': raw['closure_complete'],
        'reported_data_accumulating': obj.get('data_accumulating') is True,
        'reported_replay_ok': raw.get('replay_ok') is True,
        'reported_live_acceptance_ok': raw.get('live_acceptance_ok') is True,
        'ok': bool(admitted and raw['ok']), 'report_contract_ok': not errors,
        'contract_ok': admitted, 'admission_ok': admitted,
        'closure_complete': certified, 'closure_certified': certified,
        'data_accumulating': bool(admitted and raw['data_accumulating']),
        'business_closure_outcome': raw['business_closure_outcome'] if admitted else 'failed',
        'contract_error': 'release_closure_report_contract_invalid' if errors else '',
        'contract_violations': sorted(set(errors)),
        'disposition': disposition, 'failure_signals': signals,
        "repair_complete": False, "execution": execution,
        "exit_code": 0 if admitted else 1,
        **split_release_conclusions(obj, execution=execution, hard_errors=hard),
    }
    from eimemory.governance.release.closure_blockers import closure_blockers
    result['closure_blockers'] = closure_blockers(obj)
    # Keep admission diagnostics tied to validated producer contracts.
    result['replay_ok'] = bool(admitted and raw.get('replay_ok'))
    result['live_acceptance_ok'] = bool(admitted and raw.get('live_acceptance_ok'))
    return result


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
