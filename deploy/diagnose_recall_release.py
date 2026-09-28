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
    if not nightly:
        add('nightly_execution', 'unverified', 'input_not_supplied')
    elif type(diagnostics.get('execution_ok')) is bool:
        add('nightly_execution', 'reported_pass' if diagnostics['execution_ok'] else 'failed',
            'execution_completed' if diagnostics['execution_ok'] else 'nightly_step_failed',
            first_failed_step=code(diagnostics.get('first_failed_step'), 'unspecified'))
    elif nightly.get('ok') is False:
        add('nightly_execution', 'failed', 'legacy_aggregate_failed_root_boundary_not_recorded')
    else:
        add('nightly_execution', 'unverified', 'execution_diagnostics_missing')
    quality = obj(nightly.get('recall_quality_gate'))
    evidence = (obj(nightly.get('recall_quality_evidence')) or obj(quality.get('recall_quality_evidence'))
                or obj(diagnostics.get('recall_quality_evidence')))
    if not quality and evidence:
        # Preserve the stored diagnostic state; never fabricate quality evidence.
        state = evidence.get('status')
        quality = {'ok': state == 'sufficient', 'vacuous': state == 'insufficient',
                   'blocked_reason': 'recall_quality_evidence_incomplete' if state == 'insufficient'
                                     else 'recall_quality_gate_failed',
                   'evidence_status': state}
    if not quality:
        add('recall_quality', 'unverified', 'quality_gate_not_run_or_missing')
    elif quality.get('ok') is False:
        waiting = quality.get('blocked_reason') == 'recall_quality_evidence_incomplete' and not quality.get('blocking_metrics')
        add('recall_quality', 'waiting' if waiting else 'failed',
            code(quality.get('blocked_reason'), 'recall_quality_failed'),
            evidence_status=code(evidence.get('status') or quality.get('evidence_status'), 'unknown'),
            missing_roles=[v for v in evidence.get('missing_roles', []) if isinstance(v, str) and _CODE.fullmatch(v)][:20],
            missing_metrics=[v for v in evidence.get('missing_metrics', []) if isinstance(v, str) and _CODE.fullmatch(v)][:40])
    elif quality.get('ok') is True and quality.get('vacuous') is not True:
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
