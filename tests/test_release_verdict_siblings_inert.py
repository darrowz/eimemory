"""Standalone exact-pure-function regressions; no real Runtime or producer.

The loader registers empty package shells, then loads only four reviewed pure
project modules and the existing synthetic fixture builder. Pre-observation's
structural verdict is an explicit inert stub for summary-dispatch tests only.
Run with --root to compare the same assertions against baseline and candidate.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import types


def load_pure_modules(root):
    sys.dont_write_bytecode = True
    if any(name == 'eimemory' or name.startswith('eimemory.') for name in sys.modules):
        raise RuntimeError('run_in_fresh_process_without_project_imports')
    for name in ('eimemory', 'eimemory.governance', 'eimemory.governance.release', 'eimemory.ops'):
        package = types.ModuleType(name)
        package.__path__ = []
        sys.modules[name] = package
    names = (
        ('eimemory.governance.release.closure_contracts', 'eimemory/governance/release/closure_contracts.py'),
        ('eimemory.governance.release.closure_blockers', 'eimemory/governance/release/closure_blockers.py'),
        ('eimemory.governance.release.closure_verdict', 'eimemory/governance/release/closure_verdict.py'),
        ('eimemory.ops.release_closure_failure', 'eimemory/ops/release_closure_failure.py'),
        ('release_report_fixtures', 'tests/release_report_fixtures.py'),
    )
    loaded = {}
    for name, path in names:
        spec = importlib.util.spec_from_file_location(name, root / path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    pre_observation = types.ModuleType('eimemory.governance.release_pre_observation')
    pre_observation.pre_observation_report_ok = lambda report: True
    sys.modules[pre_observation.__name__] = pre_observation
    return loaded


def blocked(reason='production_dataset_not_ready'):
    return {'report_type': 'l5_release_closure', 'ok': False,
            'closure_complete': False, 'data_accumulating': False,
            'blocked_stage': 'readiness', 'blocked_reason': reason,
            'deployment': {'commit': 'a' * 40, 'promotion_request_id': 'receipt-1'},
            'deployment_receipt': {'release_session_id': 'receipt-1'}}


def observation_report(gaps):
    # The exact real producer and structural validator are deliberately absent.
    return {'report_type': 'code_evolution_pre_observation', 'ok': True,
            'status': 'ready_for_observation', 'closure_complete': False,
            'data_accumulating': False, 'readiness': {'gaps': gaps},
            'closure_rehearsal': {'ok': False, 'blocked_reasons': ['l5_readiness_not_l5']}}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    modules = load_pure_modules(args.root)
    verdict = modules['eimemory.governance.release.closure_verdict']
    incident = modules['eimemory.ops.release_closure_failure']
    fixtures = modules['release_report_fixtures']
    results = []
    compatibility = {}

    def case(name, operation):
        try:
            detail = operation()
            results.append({'name': name, 'passed': True, 'detail': detail})
        except Exception as error:
            results.append({'name': name, 'passed': False,
                            'error_type': type(error).__name__, 'error': str(error)})

    def has_code(signals, bucket, code):
        return any(item['code'] == code for item in signals[bucket])

    def hard_sibling(field, value):
        report = blocked()
        report['errors'] = [{'reason': 'awaiting_evidence', field: value}]
        snapshot = deepcopy(report)
        summary = verdict.summarize_release_closure(report)
        require(has_code(summary['failure_signals'], 'hard_errors', 'storage_integrity_failed'),
                'hard sibling was lost')
        require(summary['disposition'] == 'failure_detected', 'hard must dominate wait')
        require(not summary['admission_ok'] and not summary['closure_certified'] and summary['exit_code'] == 1,
                'blocked hard report must remain non-admitted')
        detected = incident.detect_release_closure_failure(report, detected_at='synthetic-time')
        require(detected['actionable'] and detected['incident'] is not None, 'incident must be actionable')
        require(detected['repair_complete'] is False, 'detection must not claim repair')
        require(report == snapshot, 'input report changed')
        return {'disposition': summary['disposition'], 'incident_created_in_memory': True}

    for field in ('contract_error', 'error', 'errors', 'gate_errors', 'blocking_metrics',
                  'blocked_reason', 'blocked_reasons', 'missing_evidence', 'gaps'):
        value = {'record': 'storage_integrity_failed'} if field == 'gate_errors' else (
            ['storage_integrity_failed'] if field in {'errors', 'blocked_reasons', 'missing_evidence', 'gaps'}
            else 'storage_integrity_failed')
        case('hard_sibling_' + field, lambda field=field, value=value: hard_sibling(field, value))

    def no_primary(primary):
        signals = verdict.failure_signals({'errors': [{primary: '', 'error': 'storage_integrity_failed'}]})
        require(has_code(signals, 'hard_errors', 'storage_integrity_failed'), 'empty label hid hard sibling')
    for primary in ('reason', 'code'):
        case('empty_' + primary + '_keeps_hard_sibling', lambda primary=primary: no_primary(primary))

    def primary_semantics(item, expected):
        signals = verdict.failure_signals({'errors': [item]})
        require([x['code'] for x in signals['waits']] == [expected], 'primary alias changed or was counted twice')
        require(not signals['hard_errors'] and not signals['diagnosis'], 'primary alias precedence changed')
        require(signals['waits'][0]['path'] == '$.errors.0', 'existing primary path changed')
    for name, item in (
        ('same_reason_code_once', {'reason': 'awaiting_evidence', 'code': 'awaiting_evidence'}),
        ('original_reason_precedes_code', {'reason': 'awaiting_evidence', 'code': 'unselected_alternate_code'}),
        ('empty_reason_falls_back_to_code', {'reason': '', 'code': 'awaiting_evidence'}),
    ):
        case(name, lambda item=item: primary_semantics(item, 'awaiting_evidence'))

    def wait_sibling():
        report = blocked()
        report['errors'] = [{'reason': 'awaiting_evidence', 'error': 'evidence_waiting'}]
        summary = verdict.summarize_release_closure(report)
        require(summary['disposition'] == 'evidence_waiting', 'ordinary wait error became hard')
        require(not summary['failure_signals']['hard_errors'], 'unexpected hard signal')
        require(incident.detect_release_closure_failure(report, detected_at='t')['incident'] is None,
                'pure waits must not create incident')
    case('legitimate_wait_sibling', wait_sibling)

    def diagnosis(sibling):
        report = blocked('not_ready' if not sibling else 'production_dataset_not_ready')
        report['errors'] = [{'reason': 'awaiting_evidence', 'error': 'not_ready'}] if sibling else [{'reason': 'not_ready'}]
        summary = verdict.summarize_release_closure(report)
        require(summary['disposition'] == 'diagnosis_required', 'diagnosis bucket changed or was lost')
        require(not summary['failure_signals']['hard_errors'], 'diagnosis became hard failure')
        detected = incident.detect_release_closure_failure(report, detected_at='t')
        require(detected['incident'] is not None and not detected['repair_eligible'], 'diagnosis repair semantics changed')
    case('pure_diagnosis_control', lambda: diagnosis(False))
    case('diagnosis_sibling_dominates_wait', lambda: diagnosis(True))

    def classification(label, expected_bucket):
        signals = verdict.failure_signals({'errors': [{'reason': label}]})
        require(has_code(signals, expected_bucket, label), 'named category changed')
        require(sum(len(v) for v in signals.values()) == 1, 'label counted more than once')
    for label, bucket in (
        ('awaiting_evidence', 'waits'), ('strict_code_evolution_receipt_required', 'waits'),
        ('not_ready', 'diagnosis'), ('unrecognized_failure_code', 'hard_errors'),
    ):
        case('category_' + label, lambda label=label, bucket=bucket: classification(label, bucket))

    def forced(field, outer):
        item = {'reason': 'awaiting_evidence', 'error': 'evidence_waiting'} if outer else {
            'reason': 'awaiting_evidence', field: 'evidence_waiting'}
        signals = verdict.failure_signals({field if outer else 'errors': [item]})
        require(has_code(signals, 'hard_errors', 'evidence_waiting'), 'force did not reach sibling')
        require(bool(signals['waits']) is (not outer), 'force changed outside its field boundary')
    for field in ('contract_error', 'blocking_metrics'):
        for outer in (False, True):
            case('force_' + field + ('_inherited' if outer else '_sibling'),
                 lambda field=field, outer=outer: forced(field, outer))

    def cycle(field):
        item = {'reason': 'awaiting_evidence'}
        item[field] = [item]
        signals = verdict.failure_signals({'errors': [item]})
        require(has_code(signals, 'hard_errors', 'cyclic_control_report'), 'diagnostic sibling cycle was lost')
    for field in ('error', 'errors', 'gaps'):
        case('cycle_' + field, lambda field=field: cycle(field))

    def ignored_metadata():
        item = {'reason': 'awaiting_evidence', 'message': 'private prose with error words',
                'samples': [{'error': 'not_control_plane'}]}
        item['metadata'] = item
        signals = verdict.failure_signals({'errors': [item]})
        require(not signals['hard_errors'] and not signals['diagnosis'], 'metadata was scanned')
        require(len(signals['waits']) == 1, 'primary was counted twice')
        require('private prose' not in json.dumps(signals), 'prose leaked into signals')
    case('inert_metadata_and_metadata_cycle_ignored', ignored_metadata)

    def reuse_mapping():
        item = {'reason': 'awaiting_evidence', 'error': 'evidence_waiting'}
        signals = verdict.failure_signals({'errors': [item, item]})
        require(not signals['hard_errors'], 'noncyclic shared mapping was called cyclic')
        require(len(signals['waits']) == 4, 'distinct paths must each be visited once')
    case('shared_mapping_not_cycle', reuse_mapping)

    def budget(count, siblings, should_exhaust):
        item = {'reason': 'awaiting_evidence', 'code': 'awaiting_evidence'}
        if siblings:
            item['error'] = 'evidence_waiting'
        signals = verdict.failure_signals({'errors': [dict(item) for _ in range(count)]})
        exhausted = has_code(signals, 'hard_errors', 'diagnostic_budget_exceeded')
        require(exhausted is should_exhaust, 'signal budget boundary or duplicate-field counting changed')
        require(sum(x['code'] == 'diagnostic_budget_exceeded' for x in signals['hard_errors']) <= 1,
                'budget sentinel duplicated')
    for name, count, siblings, exhausted in (
        ('exact_primary_budget_no_duplicate_alias', 2047, False, False),
        ('primary_budget_exhausted', 2048, False, True),
        ('sibling_budget_below_limit', 1023, True, False),
        ('sibling_budget_exhausted', 1024, True, True),
    ):
        case(name, lambda count=count, siblings=siblings, exhausted=exhausted: budget(count, siblings, exhausted))

    def deep_siblings():
        item = {'reason': 'awaiting_evidence'}
        for _ in range(20):
            item = {'reason': 'awaiting_evidence', 'errors': [item]}
        signals = verdict.failure_signals({'errors': [item]})
        require(has_code(signals, 'hard_errors', 'diagnostic_budget_exceeded'), 'deep siblings did not fail closed')
    case('sibling_depth_exhausted', deep_siblings)

    def controls(builder, name):
        report = builder()
        summary = verdict.summarize_release_closure(report)
        require(summary['admission_ok'] and summary['exit_code'] == 0, 'existing accepted control changed')
        compatibility[name] = sha256(json.dumps(summary, sort_keys=True, allow_nan=False).encode()).hexdigest()
    for name in ('complete_report', 'accumulating_report', 'wait_report'):
        case('compatibility_' + name, lambda name=name: controls(getattr(fixtures, name), name))

    def scope_unchanged():
        report = fixtures.complete_report()
        expected = {'tenant_id': 'tenant', 'agent_id': 'a', 'workspace_id': 'w', 'user_id': 'u'}
        require(verdict.summarize_release_closure(report, execution={'expected_scope': expected})['admission_ok'],
                'missing scope policy was changed')
        report['scope'] = {**expected, 'user_id': 'other'}
        require(not verdict.summarize_release_closure(report, execution={'expected_scope': expected})['admission_ok'],
                'existing present scope mismatch policy changed')
    case('scope_policy_unchanged', scope_unchanged)

    def observation(gaps, expected_hard):
        report = observation_report(gaps)
        summary = verdict.summarize_release_closure(report)
        require(bool(summary['failure_signals']['hard_errors']) is expected_hard,
                'summary leaf exception swallowed nested error or rejected direct wait')
        require(summary['admission_ok'] is (not expected_hard), 'unexpected summary admission result')
        require(not summary['closure_certified'] and not summary['repair_complete'], 'observation claimed closure/repair')
        return {'structural_validator': 'inert_always_true_stub', 'producer_executed': False}
    case('isolated_observation_direct_leaf_wait', lambda: observation(['terminal_receipt_unbound'], False))
    case('isolated_observation_nested_error_stays_hard', lambda: observation([
        {'reason': 'awaiting_evidence', 'error': 'storage_integrity_failed'}], True))
    case('isolated_observation_nested_forced_wait_stays_hard', lambda: observation([
        {'reason': 'awaiting_evidence', 'contract_error': 'evidence_waiting'}], True))
    case('isolated_observation_nested_nonforced_wait', lambda: observation([
        {'reason': 'awaiting_evidence', 'error': 'evidence_waiting'}], False))

    def rehearsal_nested_error():
        report = observation_report(['terminal_receipt_unbound'])
        report['closure_rehearsal']['blocked_reasons'] = [
            {'reason': 'l5_readiness_not_l5', 'error': 'l5_readiness_not_l5'}]
        summary = verdict.summarize_release_closure(report)
        require(has_code(summary['failure_signals'], 'hard_errors', 'l5_readiness_not_l5'),
                'rehearsal nested explicit error was softened')
        require(not summary['admission_ok'], 'nested rehearsal error admitted')
    case('isolated_observation_rehearsal_nested_error', rehearsal_nested_error)

    def placeholders():
        report = blocked()
        report['live_acceptance'] = {'ok': False, 'status': 'not_run', 'reason': 'upstream_gate_not_run'}
        require(verdict.summarize_release_closure(report)['disposition'] == 'evidence_waiting', 'placeholder semantics changed')
        report['live_acceptance']['error'] = 'storage_integrity_failed'
        require(verdict.summarize_release_closure(report)['disposition'] == 'failure_detected', 'placeholder hid actual error')
    case('placeholder_reason_only_exception', placeholders)

    def redaction():
        report = blocked()
        report['errors'] = [{'reason': 'awaiting_evidence', 'error': 'token=synthetic-private-value'}]
        signals = verdict.failure_signals(report)
        require(has_code(signals, 'hard_errors', 'reported_error'), 'explicit prose error was lost')
        require('synthetic-private-value' not in json.dumps(signals), 'raw error exposed')
    case('explicit_error_redacted', redaction)

    allowed = {'eimemory', 'eimemory.governance', 'eimemory.governance.release', 'eimemory.ops',
               'eimemory.governance.release_pre_observation', *modules.keys()}
    unexpected = sorted(name for name in sys.modules if (name == 'eimemory' or name.startswith('eimemory.')) and name not in allowed)
    require(not unexpected, 'unrelated project module imported: ' + repr(unexpected))
    failed = [item for item in results if not item['passed']]
    output = {'schema': 'release-verdict-pure-regressions.v1', 'root': str(args.root),
              'method': 'Exact reviewed pure modules via inert package shells; existing synthetic fixture builder; pre-observation structural contract stubbed only in summary-dispatch cases',
              'project_module_allowlist': sorted(allowed), 'unrelated_project_imports': unexpected,
              'real_Runtime_producer_terminal_provider_execution': False,
              'source_sha256': sha256((args.root / 'eimemory/governance/release/closure_verdict.py').read_bytes()).hexdigest(),
              'test_count': len(results), 'passed': len(results) - len(failed), 'failed': len(failed),
              'compatibility_summary_hashes': compatibility, 'cases': results}
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'tests': len(results), 'passed': len(results) - len(failed), 'failed': len(failed),
                      'failed_cases': [item['name'] for item in failed], 'output': str(args.output)}))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
