#!/usr/bin/env python3
"""Correlate supplied release/recall reports without executing acceptance.

The output describes report evidence, not independently authenticated authority.
Missing inputs stay unverified. No source, database, or checkpoint is modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.core.strict_json import loads
from eimemory.scheduler.controlled_acceptance_diagnostics import _historical_business_agents
from eimemory.scheduler.result_contract import (
    _aggregate_nightly_ok, _authority_boundary_failed, _has_report_failure,
)

MAX_BYTES = 16 * 1024 * 1024
_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,160}')


def read_report(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError('report_file_not_bounded_regular_file')
        raw = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('report_changed_during_read')
    value = loads(raw, max_bytes=MAX_BYTES, max_depth=64)
    if not isinstance(value, dict):
        raise ValueError('report_root_must_be_object')
    return value


def obj(value):
    return value if isinstance(value, dict) else {}


def code(value, fallback):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def identity(report):
    for candidate in (obj(report.get('current_release')), obj(report.get('release_identity')),
                      obj(report.get('deployment')), report):
        commit = candidate.get('release_commit') or candidate.get('commit') or candidate.get('candidate_commit')
        if commit:
            return (commit, candidate.get('deployment_receipt_id') or candidate.get('receipt_id')
                    or candidate.get('promotion_request_id'), candidate.get('release_session_id') or candidate.get('session_id'))
    return ('', '', '')


def _historical_business_reported(nightly, diagnostics, nightly_identities, *, expected_commit,
                                  expected_receipt, expected_session):
    """Correlate the native scheduler's verified BQ, without authenticating it.

    The independent verifier ran in nightly. This read-only tool still reports
    supplied evidence only, and must reject contradictory raw execution gates.
    """
    controlled = obj(diagnostics.get('controlled_production_acceptance'))
    qualification = obj(controlled.get('business_qualification'))
    evidence = obj(diagnostics.get('recall_quality_evidence'))
    release = obj(qualification.get('release_identity'))
    agents = _historical_business_agents(qualification.get('agents'))
    count = sum(agent['sample_count'] for agent in agents.values())
    release_key = identity(qualification)
    if not (
        diagnostics.get('schema') == 'nightly_execution_diagnostics.v1'
        and diagnostics.get('execution_ok') is True and diagnostics.get('failed_steps') == []
        and diagnostics.get('recall_quality_accepted') is True
        and diagnostics.get('recall_quality_acceptance_basis') == 'historical_business_memory_controlled'
        and _aggregate_nightly_ok(nightly, [])
        and not any(_has_report_failure(value) or _authority_boundary_failed(value)
                    for value in (nightly, diagnostics, controlled))
        and controlled.get('accepted') is True and controlled.get('status') == 'accepted'
        and controlled.get('mode') == 'controlled_existing_memory'
        and controlled.get('acceptance_purpose') == 'historical_business_qualification'
        and controlled.get('certifies_natural_queries') is False
        and type(controlled.get('natural_samples_created')) is int and controlled['natural_samples_created'] == 0
        and set(qualification) == {'schema', 'purpose', 'accepted', 'business_origin', 'validation_method',
                                  'sample_count', 'pass_count', 'fail_count', 'agents', 'policy_digest',
                                  'record_id', 'release_identity'}
        and qualification.get('schema') == 'historical_business_qualification.v1'
        and qualification.get('purpose') == 'historical_business_qualification'
        and qualification.get('accepted') is True and agents
        and qualification.get('business_origin') == 'historical_business'
        and qualification.get('validation_method') == 'controlled_recall'
        and isinstance(qualification.get('policy_digest'), str)
        and re.fullmatch('[0-9a-f]{64}', qualification['policy_digest'])
        and qualification.get('policy_digest') == controlled.get('policy_digest')
        and isinstance(qualification.get('record_id'), str) and qualification['record_id']
        and qualification.get('record_id') == controlled.get('record_id')
        and set(release) == {'release_commit', 'release_version', 'deployment_receipt_id', 'release_session_id'}
        and all(isinstance(value, str) and 1 <= len(value) <= 256 and value.isprintable()
                for value in release.values())
        and release_key[0] == expected_commit
        and (not expected_receipt or release_key[1] == expected_receipt)
        and (not expected_session or release_key[2] == expected_session)
        and release_key in nightly_identities
        and all(all(not reported or reported == expected for reported, expected in zip(key, release_key))
                for key in nightly_identities)
        and type(qualification.get('sample_count')) is int and qualification['sample_count'] == count
        and type(qualification.get('pass_count')) is int and qualification['pass_count'] == count
        and type(qualification.get('fail_count')) is int and qualification['fail_count'] == 0
        and evidence.get('evaluation_contract') == qualification['schema']
        and evidence.get('formal_business_evidence') is True and evidence.get('release_authority_verified') is True
        and evidence.get('authority_record_id') == qualification['record_id']
        and evidence.get('policy_digest') == qualification['policy_digest']
        and all(type(evidence.get(key)) is int and evidence[key] == qualification[key]
                for key in ('sample_count', 'pass_count', 'fail_count'))
    ):
        return {}
    return qualification


def diagnose(*, expected_commit: str, reports: dict, expected_receipt: str = '', expected_session: str = '') -> dict:
    if not re.fullmatch('[0-9a-f]{40}', expected_commit):
        raise ValueError('expected_commit_must_be_full_lowercase_sha')
    boundaries = []
    def add(name, state, reason, **details):
        boundaries.append({'boundary': name, 'state': state, 'reason': reason, **details})
    def external(name, identity_required=True, full_authority=False):
        report = obj(reports.get(name))
        if not report:
            add(name, 'unverified', 'input_not_supplied'); return
        commit, receipt, session = identity(report)
        if commit and commit != expected_commit:
            add(name, 'failed', 'release_commit_mismatch'); return
        if full_authority:
            if expected_receipt and receipt and receipt != expected_receipt:
                add(name, 'failed', 'release_receipt_mismatch'); return
            if expected_session and session and session != expected_session:
                add(name, 'failed', 'release_session_mismatch'); return
        if report.get('ok') is False:
            add(name, 'failed', code(report.get('reason') or report.get('blocked_reason') or report.get('error'), name + '_failed')); return
        if report.get('ok') is not True or (identity_required and not commit):
            add(name, 'unverified', 'success_or_release_identity_missing'); return
        if full_authority and not (expected_receipt and expected_session and receipt and session):
            add(name, 'unverified', 'receipt_session_binding_not_supplied'); return
        if name == 'lineage' and (report.get('validated') is not True or report.get('compatible') is not True):
            add(name, 'unverified', 'lineage_validation_not_confirmed'); return
        add(name, 'reported_pass', 'bound_report_passed')
    external('release_source')
    external('health')
    external('binding', full_authority=True)
    external('lineage', full_authority=True)
    nightly = obj(reports.get('nightly'))
    if obj(nightly.get('runs')).get('nightly') is not None:
        nightly = obj(nightly['runs']['nightly'])
    diagnostics = obj(nightly.get('nightly_diagnostics'))
    if not diagnostics:
        diagnostics = obj(obj(nightly.get('supervisor_summary')).get('nightly_diagnostics'))
    nightly_identities = [identity(nightly), identity(obj(nightly.get('supervisor_summary')))]
    nightly_identities = [value for value in nightly_identities if value[0]]
    nightly_binding_state = ''
    nightly_binding_reason = ''
    if any(commit != expected_commit for commit, _, _ in nightly_identities):
        nightly_binding_state, nightly_binding_reason = 'failed', 'release_commit_mismatch'
    elif not nightly_identities:
        nightly_binding_state, nightly_binding_reason = 'unverified', 'nightly_release_identity_missing'
    elif expected_receipt and any(receipt and receipt != expected_receipt for _, receipt, _ in nightly_identities):
        nightly_binding_state, nightly_binding_reason = 'failed', 'release_receipt_mismatch'
    elif expected_session and any(session and session != expected_session for _, _, session in nightly_identities):
        nightly_binding_state, nightly_binding_reason = 'failed', 'release_session_mismatch'
    elif not any((not expected_receipt or receipt == expected_receipt)
                 and (not expected_session or session == expected_session)
                 for _, receipt, session in nightly_identities):
        nightly_binding_state, nightly_binding_reason = 'unverified', 'receipt_session_binding_not_supplied'
    if not nightly:
        add('nightly_execution', 'unverified', 'input_not_supplied')
    elif nightly_binding_reason:
        add('nightly_execution', nightly_binding_state, nightly_binding_reason,
            first_failed_step=code(diagnostics.get('first_failed_step'), 'unspecified'))
    elif (_has_report_failure(nightly) or _authority_boundary_failed(nightly)
          or not _aggregate_nightly_ok(nightly, [])):
        add('nightly_execution', 'failed', 'raw_nightly_execution_or_authority_failed',
            first_failed_step=code(diagnostics.get('first_failed_step'), 'unspecified'))
    elif type(diagnostics.get('execution_ok')) is bool:
        add('nightly_execution', 'reported_pass' if diagnostics['execution_ok'] else 'failed',
            'execution_completed' if diagnostics['execution_ok'] else 'nightly_step_failed',
            first_failed_step=code(diagnostics.get('first_failed_step'), 'unspecified'))
    elif nightly.get('ok') is False:
        add('nightly_execution', 'failed', 'legacy_aggregate_failed_root_boundary_not_recorded')
    else:
        add('nightly_execution', 'unverified', 'execution_diagnostics_missing')
    # Scheduler completion precedes CLI identity repair. Only a matching
    # persisted command ending can describe the whole command's exit status.
    supervisor = obj(nightly.get('supervisor_summary')) or nightly
    command = obj(nightly.get('command_execution')) or obj(supervisor.get('command_execution'))
    identity_command = obj(command.get('identity_repair'))
    if nightly_binding_reason:
        add('nightly_command', nightly_binding_state, nightly_binding_reason)
    elif command.get('status') != 'recorded':
        if command.get('ok') is False and command.get('first_failed_phase') == 'command_outcome_persistence':
            add('nightly_command', 'failed', 'nightly_command_outcome_persistence_failed')
        else:
            add('nightly_command', 'unverified', 'nightly_command_outcome_not_recorded')
    elif (command.get('schema') != 'nightly_command_outcome.v1' or not command.get('record_id')
          or not supervisor.get('run_id') or not supervisor.get('record_id')
          or command.get('run_id') != supervisor['run_id']
          or command.get('supervisor_record_id') != supervisor['record_id']):
        add('nightly_command', 'failed', 'nightly_command_run_binding_mismatch')
    elif not all(command.get(key) for key in ('release_commit', 'deployment_receipt_id', 'release_session_id')):
        add('nightly_command', 'unverified', 'nightly_command_release_identity_missing')
    elif command.get('release_commit') != expected_commit:
        add('nightly_command', 'failed', 'release_commit_mismatch')
    elif any(command.get(key, '') != supervisor.get(key, '') for key in (
            'release_commit', 'release_version', 'deployment_receipt_id', 'release_session_id')):
        add('nightly_command', 'failed', 'nightly_command_release_binding_mismatch')
    elif (any(type(command.get(key)) is not bool for key in (
              'ok', 'scheduler_ok', 'scheduler_execution_ok', 'identity_repair_ok'))
          or any(type(identity_command.get(key)) is not bool for key in ('ok', 'exception_raised'))
          or command['scheduler_execution_ok'] != diagnostics.get('execution_ok')
          or command['scheduler_ok'] != supervisor.get('ok')
          or command['identity_repair_ok'] != identity_command['ok']
          or (identity_command['ok'] and identity_command['exception_raised'])
          or command['ok'] != (command['scheduler_ok'] and command['scheduler_execution_ok']
                               and command['identity_repair_ok'])
          or type(command.get('exit_code')) is not int
          or command['exit_code'] != (0 if command['ok'] else 1)
          or command.get('first_failed_phase') != ('scheduler' if not command['scheduler_ok']
                  or not command['scheduler_execution_ok'] else 'identity_repair'
                  if not command['identity_repair_ok'] else '')):
        add('nightly_command', 'failed', 'nightly_command_outcome_inconsistent')
    else:
        add('nightly_command', 'reported_pass' if command['ok'] else 'failed',
            'command_completed' if command['ok'] else 'nightly_command_failed',
            exit_code=command['exit_code'],
            first_failed_phase=command['first_failed_phase'])
    quality = obj(nightly.get('recall_quality_gate'))
    evidence = (obj(nightly.get('recall_quality_evidence')) or obj(quality.get('recall_quality_evidence'))
                or obj(diagnostics.get('recall_quality_evidence')))
    historical = _historical_business_reported(
        nightly, diagnostics, nightly_identities, expected_commit=expected_commit,
        expected_receipt=expected_receipt, expected_session=expected_session,
    ) if not nightly_binding_reason else {}
    if not quality and evidence:
        # Preserve the stored diagnostic state; never fabricate quality evidence.
        state = evidence.get('status')
        quality = {'ok': state == 'sufficient', 'vacuous': state == 'insufficient',
                   'blocked_reason': 'recall_quality_evidence_incomplete' if state == 'insufficient'
                                     else 'recall_quality_gate_failed',
                   'evidence_status': state}
    if _has_report_failure(quality) or _authority_boundary_failed(quality):
        add('recall_quality', 'failed', 'recall_quality_execution_or_authority_failed')
    elif historical:
        add('recall_quality', 'reported_pass', 'historical_business_quality_reported_not_release_certification',
            acceptance_basis='historical_business_memory_controlled', business_origin='historical_business',
            validation_method='controlled_recall', sample_count=historical['sample_count'],
            natural_online_accepted=diagnostics.get('natural_recall_quality_accepted') is True,
            natural_online_waiting=bool(diagnostics.get('natural_evidence_waits')))
    elif not quality:
        add('recall_quality', 'unverified', 'quality_gate_not_run_or_missing')
    elif quality.get('ok') is False:
        hard_failure = _has_report_failure(quality) or _authority_boundary_failed(quality)
        waiting = quality.get('blocked_reason') == 'recall_quality_evidence_incomplete' and not hard_failure
        add('recall_quality', 'waiting' if waiting else 'failed',
            'recall_quality_execution_or_authority_failed' if hard_failure
            else code(quality.get('blocked_reason'), 'recall_quality_failed'),
            evidence_status=code(evidence.get('status') or quality.get('evidence_status'), 'unknown'),
            missing_roles=[v for v in evidence.get('missing_roles', []) if isinstance(v, str) and _CODE.fullmatch(v)][:20],
            missing_metrics=[v for v in evidence.get('missing_metrics', []) if isinstance(v, str) and _CODE.fullmatch(v)][:40])
    elif quality.get('ok') is True and quality.get('vacuous') is not True:
        if nightly_binding_reason:
            add('recall_quality', nightly_binding_state, nightly_binding_reason)
        else:
            add('recall_quality', 'reported_pass', 'quality_report_passed_not_release_certification')
    else:
        add('recall_quality', 'unverified', 'vacuous_or_malformed_quality_report')
    first = lambda states: next((r['boundary'] for r in boundaries if r['state'] in states), '')
    return {'schema': 'recall_release_boundary_diagnosis.v1', 'expected_commit': expected_commit,
            'analysis_complete': True, 'closure_certified': False, 'independent_authority_verified': False,
            'first_blocking_boundary': first({'failed', 'unverified', 'waiting'}),
            'first_failed_boundary': first({'failed'}), 'first_unverified_boundary': first({'unverified'}),
            'boundaries': boundaries, 'writer_attribution': 'not_established',
            'database_mutated': False, 'scene_cleanup_performed': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--expected-commit', required=True)
    p.add_argument('--expected-receipt', default='')
    p.add_argument('--expected-session', default='')
    for name in ('release-source', 'health', 'binding', 'lineage', 'nightly'):
        p.add_argument('--'+name, type=Path)
    args = p.parse_args(argv)
    try:
        reports = {name: read_report(path) for name in ('release_source', 'health', 'binding', 'lineage', 'nightly')
                   if (path := getattr(args, name)) is not None}
        result = diagnose(expected_commit=args.expected_commit, reports=reports,
                          expected_receipt=args.expected_receipt, expected_session=args.expected_session)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        # 0 means reports analyzed, not acceptance passed. Return 1 while any
        # boundary is failed, waiting or unverified, so pipelines cannot bless it.
        return 1 if result['first_blocking_boundary'] else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'analysis_complete': False, 'closure_certified': False,
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
