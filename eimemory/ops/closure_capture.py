"""Capture and process one immutable input snapshot; never re-read for classification."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable

from eimemory.core.strict_json import loads
from eimemory.governance.release.closure_verdict import summarize_release_closure, report_digest
from eimemory.ops.release_closure_failure import record_release_closure_failure
from eimemory.storage.atomic_file import atomic_write_bytes, atomic_write_json

MAX_REPORT_BYTES = 16 * 1024 * 1024


def read_source(path: Path) -> bytes:
    named = path.lstat()
    if stat.S_ISLNK(named.st_mode) or getattr(named, 'st_file_attributes', 0) & 0x400:
        raise ValueError('closure_source_link_not_allowed')
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_REPORT_BYTES:
            raise ValueError('closure_source_not_bounded_regular_file')
        raw = handle.read(MAX_REPORT_BYTES + 1)
        after = os.fstat(handle.fileno())
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError('closure_source_too_large')
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or len(raw) != after.st_size:
            raise ValueError('closure_source_changed_during_read')
    return raw


def _capture_directory(root: Path) -> Path:
    root = root.absolute()
    # The operator owns the parent chain; do not follow links or write into a release.
    if root.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError('capture_must_be_outside_release')
    for parent in (root, *root.parents):
        if parent.is_symlink(): raise ValueError('capture_parent_symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('capture_root_not_private')
    return Path(tempfile.mkdtemp(prefix='closure-', dir=root))


def process_capture(
    source: Path, *, evidence_dir: Path, scope: dict,
    expected_commit: str = '', attempt_id: str = '', closure_exit_status: int | None = None,
    runtime_factory: Callable[[], Any] | None = None, inspect_only: bool = False,
) -> dict:
    if expected_commit and re.fullmatch('[0-9a-f]{40}', expected_commit) is None:
        raise ValueError('expected_commit_invalid')
    if len(attempt_id) > 200 or any(ord(c) < 32 for c in attempt_id):
        raise ValueError('attempt_id_invalid')
    capture_dir = _capture_directory(evidence_dir)
    context = {'expected_commit': expected_commit, 'expected_scope': dict(scope), 'attempt_id': attempt_id,
               'closure_exit_status': closure_exit_status}
    raw = None
    source_archived = False
    input_error = ''
    try:
        raw = read_source(source)
        atomic_write_bytes(capture_dir/'source.bin', raw)
        source_archived = True
        report = loads(raw, max_bytes=MAX_REPORT_BYTES, max_depth=64)
        if not isinstance(report, dict): raise ValueError('closure_output_not_object')
    except (OSError, ValueError, UnicodeError) as exc:
        input_error = type(exc).__name__
        # A read/parse error is itself a failed boundary, never an evidence wait.
        # Only caller-supplied expected identity is used; no receipt is invented.
        report = {'report_type': 'l5_release_closure', 'ok': False,
                  'closure_complete': False, 'data_accumulating': False,
                  'blocked_stage': 'report_read', 'blocked_reason': 'closure_output_unreadable',
                  'error': 'closure_output_unreadable', 'deployment': {'commit': expected_commit}}
    context['source_sha256'] = sha256(raw).hexdigest() if raw is not None else ''
    summary = summarize_release_closure(report, execution=context)
    capture = {
        'schema': 'release_closure_capture.v1', 'capture_id': capture_dir.name,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_sha256': sha256(raw).hexdigest() if raw is not None else '',
        'source_size': len(raw) if raw is not None else None,
        'report_digest': summary['report_digest'], 'decision_digest': report_digest(summary),
        'attempt_id': attempt_id,
        'expected_commit': expected_commit, 'scope': scope, 'input_error_type': input_error,
        'path': str(capture_dir), 'execution': context,
    }
    atomic_write_json(capture_dir/'summary.json', summary)
    atomic_write_json(capture_dir/'capture.json', capture)
    result = {'recording_ok': True, 'incident_record_id': '', 'status': summary['disposition'],
              'repair_status': 'not_required', 'repair_complete': False}
    runtime = None
    try:
        if summary['disposition'] in {'failure_detected', 'diagnosis_required'}:
            if inspect_only:
                result.update(recording_ok=False, status='inspection_only', repair_status='not_recorded')
            else:
                if runtime_factory is None:
                    from eimemory.api.runtime import Runtime
                    runtime_factory = Runtime.create
                runtime = runtime_factory()
                recorded = record_release_closure_failure(
                    runtime, scope=scope, closure_report=report, detected_at=capture['created_at'],
                    execution=context, capture=capture,
                )
                if (recorded['report_digest'] != summary['report_digest']
                        or recorded['validation']['disposition'] != summary['disposition']
                        or recorded['validation']['failure_signals'] != summary['failure_signals']):
                    raise ValueError('closure_decision_snapshot_mismatch')
                result = {key: recorded[key] for key in (
                    'recording_ok', 'incident_record_id', 'status', 'repair_status', 'repair_complete')}
                if not result['incident_record_id']:
                    raise ValueError('actionable_incident_not_persisted')
    except Exception as exc:
        result.update(recording_ok=False, status='incident_recording_failed',
                      repair_status='persistence_failed', error_type=type(exc).__name__)
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                result.update(recording_ok=False, status='runtime_close_failed',
                              error_type=type(exc).__name__)
    output = {
        **summary, **result, 'capture_saved': source_archived, 'capture': capture,
        'gate_exit_code': summary['exit_code'],
        'exit_code': summary['exit_code'] if result['recording_ok'] and source_archived else 2,
    }
    atomic_write_json(capture_dir/'result.json', output)
    return output
