"""Native latest-capture fence for first-incident authority, not quality proof.

Every begun capture supersedes its predecessor, including failed/in-flight
captures. Immutable reports and incidents are retained. A marker never proves
CPA acceptance, provenance novelty, a deployment receipt or elapsed observation.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
import os
import stat

from eimemory.core.strict_json import loads
from eimemory.governance.release.closure_verdict import report_digest, summarize_release_closure
from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
from eimemory.governance.evolution.code_automation_policy_issue import _policy_lock
from eimemory.storage.atomic_file import atomic_write_json


def _key(scope: Mapping[str, Any], commit: str) -> str:
    return report_digest({'scope': dict(scope), 'commit': commit})


def _private_json(path: Path) -> dict:
    if _v2_no_symlink(path, missing_ok=False):
        raise ValueError('capture_path_invalid')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == 'posix' and (info.st_uid != os.geteuid()
                                      or stat.S_IMODE(info.st_mode) & 0o077))):
        raise ValueError('capture_file_not_private')
    from eimemory.ops.closure_capture import read_source
    value = loads(read_source(path), max_bytes=16*1024*1024, max_depth=64)
    if not isinstance(value, dict):
        raise ValueError('capture_object_required')
    return value


def capture_marker(root: Path, scope: Mapping[str, Any], commit: str) -> Path:
    return root / ('.current-' + _key(scope, commit) + '.json')


def begin_current_capture(capture_dir: Path, *, scope: Mapping[str, Any], commit: str) -> None:
    marker = capture_marker(capture_dir.parent, scope, commit)
    with _policy_lock(marker.with_suffix('.lock')):
        atomic_write_json(marker, {'schema': 'code_automation_current_capture.v1',
                                  'capture_id': capture_dir.name, 'scope': dict(scope),
                                  'commit': commit, 'complete': False})


def finish_current_capture(capture_dir: Path, output: Mapping[str, Any]) -> None:
    capture = output['capture']
    marker = capture_marker(capture_dir.parent, capture['scope'], capture['expected_commit'])
    with _policy_lock(marker.with_suffix('.lock')):
        current = _private_json(marker)
        # An older slow producer cannot overwrite a newer capture's authority.
        if current.get('capture_id') != capture_dir.name:
            return
        atomic_write_json(marker, {**current, 'complete': True,
                                  'result_digest': report_digest(dict(output))})


def _current_snapshot(runtime: Any, *, scope: Any, release: Any) -> tuple[dict, dict, dict, dict]:
    root = Path(runtime.store.root) / 'logs' / 'release-closure-captures'
    if _v2_no_symlink(root / '.authority-path-check', missing_ok=True):
        raise ValueError('current_capture_root_unverified')
    info = root.stat()
    if (not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and
            (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077))):
        raise ValueError('current_capture_root_not_private')
    marker = _private_json(capture_marker(root, asdict(scope), release.commit))
    if (marker.get('schema') != 'code_automation_current_capture.v1'
            or marker.get('scope') != asdict(scope) or marker.get('commit') != release.commit
            or marker.get('complete') is not True):
        raise ValueError('current_capture_incomplete')
    name = marker.get('capture_id')
    if not isinstance(name, str) or not name.startswith('closure-') or Path(name).name != name:
        raise ValueError('current_capture_identity_invalid')
    directory = root / name
    capture = _private_json(directory / 'capture.json')
    summary = _private_json(directory / 'summary.json')
    result = _private_json(directory / 'result.json')
    execution = capture.get('execution')
    if (capture.get('path') != str(directory) or capture.get('capture_id') != name
            or capture.get('scope') != asdict(scope) or capture.get('expected_commit') != release.commit
            or result.get('capture') != capture or result.get('recording_ok') is not True
            or marker.get('result_digest') != report_digest(result)
            or not isinstance(execution, dict) or execution.get('expected_scope') != asdict(scope)
            or execution.get('expected_commit') != release.commit
            or execution.get('attempt_id') != capture.get('attempt_id')
            or execution.get('source_sha256') != capture.get('source_sha256')):
        raise ValueError('current_capture_snapshot_mismatch')
    from eimemory.ops.closure_capture import read_source
    raw = read_source(directory / 'source.bin')
    if sha256(raw).hexdigest() != capture.get('source_sha256') or len(raw) != capture.get('source_size'):
        raise ValueError('current_capture_source_mismatch')
    report = loads(raw, max_bytes=16*1024*1024, max_depth=64)
    if not isinstance(report, dict):
        raise ValueError('current_capture_report_invalid')
    actual = summarize_release_closure(report, execution=capture['execution'])
    if (actual != summary or report_digest(actual) != capture.get('decision_digest')
            or actual.get('report_digest') != capture.get('report_digest')):
        raise ValueError('current_capture_superseded_or_unverified')
    deployment = report.get('deployment') or {}
    receipt = report.get('deployment_receipt') or {}
    if (str(deployment.get('promotion_request_id') or receipt.get('promotion_request_id') or '') != release.receipt_id
            or str(receipt.get('release_session_id') or deployment.get('release_session_id') or '') != release.session_id):
        raise ValueError('current_capture_release_authority_mismatch')
    return marker, capture, result, actual


def current_closure_incident_id(runtime: Any, *, scope: Any, release: Any) -> str | None:
    """None means legacy/manual-only; a present invalid marker fails closed."""
    if not hasattr(runtime.store, 'root'):
        return None
    root = Path(runtime.store.root) / 'logs' / 'release-closure-captures'
    if not os.path.lexists(capture_marker(root, asdict(scope), release.commit)):
        return None
    _, _, result, actual = _current_snapshot(runtime, scope=scope, release=release)
    if actual['disposition'] in {'failure_detected', 'diagnosis_required'}:
        if not result.get('incident_record_id'):
            raise ValueError('current_capture_incident_unverified')
        return result['incident_record_id']
    return ''


def verify_current_closure_capture(runtime: Any, *, scope: Any, release: Any,
                                   record: Any, incident: Mapping[str, Any]) -> dict:
    """Require the exact latest completed native snapshot, then recompute it."""
    marker, capture, result, actual = _current_snapshot(runtime, scope=scope, release=release)
    if (actual.get('disposition') != 'failure_detected' or result.get('incident_record_id') != record.record_id
            or record.content.get('capture') != capture):
        raise ValueError('current_capture_superseded_or_unverified')
    identity = record.content.get('detector_report', {}).get('identity', {})
    if (identity.get('source_sha256') != capture['source_sha256']
            or identity.get('report_digest') != capture['report_digest']
            or identity.get('receipt_id') != release.receipt_id
            or identity.get('release_session_id') != release.session_id):
        raise ValueError('current_capture_release_authority_mismatch')
    return {'marker_digest': report_digest(marker), 'capture_id': capture['capture_id'],
            'source_sha256': capture['source_sha256'], 'decision_digest': capture['decision_digest']}
