#!/usr/bin/env python3
"""Preserve, validate, classify and register the same closure output snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.ops.closure_capture import process_capture, read_source
from eimemory.core.strict_json import loads


def _read_report(path: Path) -> dict:
    value = loads(read_source(path), max_bytes=16*1024*1024, max_depth=64)
    if not isinstance(value, dict): raise ValueError('closure_output_not_object')
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True, type=Path)
    parser.add_argument('--scope-agent', required=True)
    parser.add_argument('--scope-workspace', required=True)
    parser.add_argument('--scope-user', required=True)
    parser.add_argument('--scope-tenant', default='default')
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--expected-commit', default='')
    parser.add_argument('--attempt-id', default='')
    parser.add_argument('--closure-exit-status', type=int)
    parser.add_argument('--enforce-gate', action='store_true')
    parser.add_argument('--inspect-only', action='store_true')
    args = parser.parse_args(argv)
    evidence = args.evidence_dir
    if evidence is None:
        root = os.environ.get('EIMEMORY_ROOT')
        if not root: parser.error('--evidence-dir or EIMEMORY_ROOT is required')
        evidence = Path(root)/'state'/'release-closure-captures'
    try:
        result = process_capture(
            args.path, evidence_dir=evidence,
            scope={'tenant_id': args.scope_tenant, 'agent_id': args.scope_agent,
                   'workspace_id': args.scope_workspace, 'user_id': args.scope_user},
            expected_commit=args.expected_commit, attempt_id=args.attempt_id,
            closure_exit_status=args.closure_exit_status, inspect_only=args.inspect_only,
        )
    except Exception as exc:
        # A storage/runtime failure is observable and never turns into a benign wait.
        result = {'ok': False, 'recording_ok': False, 'capture_saved': False,
                  'status': 'closure_capture_failed', 'error_type': type(exc).__name__,
                  'incident_record_id': '', 'repair_complete': False,
                  'business_closure_outcome': 'failed', 'exit_code': 2}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, allow_nan=False))
    return result['exit_code'] if args.enforce_gate else (0 if result['recording_ok'] else 2)


if __name__ == '__main__':
    raise SystemExit(main())
