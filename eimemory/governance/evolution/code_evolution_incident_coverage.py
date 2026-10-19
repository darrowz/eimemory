"""Native HMAC-backed SQLite ingress diagnostics, independent of L5 consent.

This is a real watch/nightly collector, not an alternate incident producer or
an authorization source. Unknown external/free-text provenance stays unknown.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import sqlite3

from eimemory.contracts.code_incident_coverage import digest, exact_scope, unknown
from eimemory.governance.learning.effect_policy import seal, verify
from eimemory.models.records import ScopeRef
from eimemory.storage.code_incident_coverage_store import CodeIncidentCoverageStore

_PRODUCER_PATHS = (
    'eimemory/contracts/code_incident_coverage.py',
    'eimemory/contracts/code_incident_provenance.py',
    'eimemory/storage/code_incident_coverage_store.py',
    'eimemory/storage/migrations/code_incident_coverage.py',
    'eimemory/storage/sqlite_store.py',
    'eimemory/governance/evolution/code_evolution_incident_coverage.py',
    'eimemory/governance/evolution/code_automation_policy_incidents.py',
    'eimemory/governance/evolution/system_code_maintenance.py',
    'eimemory/governance/evolution/code_maintenance.py',
    'eimemory/ops/runtime_identity_drift.py',
    'eimemory/ops/release_closure_failure.py',
    'eimemory/ops/system_code_repair_failure.py',
)


def producer_identity() -> str:
    root = Path(__file__).resolve().parents[3]
    values = {}
    for name in _PRODUCER_PATHS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('incident_coverage_producer_source_unavailable')
        values[name] = sha256(path.read_bytes()).hexdigest()
    return digest(values)


def _owner(scope):
    return exact_scope(asdict(scope) if isinstance(scope, ScopeRef) else scope)


def checkpoint_native_ingress(runtime, *, scope):
    """Called before real detector work; no bootstrap when apply is disabled."""
    try:
        return CodeIncidentCoverageStore(runtime.store).checkpoint(
            _owner(scope), producer_digest=producer_identity(), seal=seal, verify=verify)
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return unknown('checkpoint_unavailable:' + type(exc).__name__)


def observe_native_incident(runtime, *, scope, record_id):
    """Bind only a real already-persisted incident to its actual journal entry."""
    if not isinstance(record_id, str) or not record_id:
        return unknown('native_incident_not_present')
    try:
        from eimemory.governance.evolution.code_automation_policy_incidents import _trusted_incident_identity
        owner = _owner(scope)
        def checked(sqlite):
            record = sqlite.get_by_id(record_id, scope=ScopeRef.from_dict(owner), exact_scope=True)
            report = getattr(record, 'content', {}).get('detector_report', {})
            commit = report.get('release_commit') or report.get('expected_commit')
            if record is None or (not _trusted_incident_identity(record, commit)
                    and not _trusted_incident_identity(record, commit, diagnosis=True)):
                raise ValueError('incident_coverage_native_source_unverified')
            return digest(record.to_dict())
        expected = runtime.store.read_consistent(checked)
        return CodeIncidentCoverageStore(runtime.store).observe(
            owner, record_id, producer_digest=producer_identity(), seal=seal, verify=verify,
            expected_payload_digest=expected)
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return unknown('observation_unavailable:' + type(exc).__name__)


def read_native_ingress_coverage(runtime, *, scope, record_id=''):
    """Read-only enrichment. This never starts coverage or an observation."""
    if not record_id:
        return unknown('qualifying_incident_not_present')
    try:
        return CodeIncidentCoverageStore(runtime.store).read_observation(
            _owner(scope), record_id, producer_digest=producer_identity(), verify=verify)
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return unknown('readback_unavailable:' + type(exc).__name__)
