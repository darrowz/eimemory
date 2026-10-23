"""Restore the vector projection before a read-only readiness accept wait.

Post-deploy CPA runs while runtime-manager still holds background units,
including eimemory-vector-sync. Burning the accept budget against a paused
worker reports authority_revision_stale forever. Wait for the storage-release
guard to clear, catch up in-process, then let the barrier observe.

The candidate readiness barrier stays read-only: it never embeds or syncs.
"""
from __future__ import annotations

import stat
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from time import monotonic, sleep
from typing import Any

SCHEMA = 'projection_acceptance_restore.v1'
# Separate from the readiness accept budget. Storage markers usually clear
# before business admission; catch-up after a held sync is typically seconds.
DEFAULT_WAIT_SECONDS = 120.0
MAX_WAIT_SECONDS = 300.0
POLL_SECONDS = 0.5
# Match runtime-manager busy markers (storage release + restore journal).
_MARKERS = (
    'storage-release-transaction.json',
    '.storage-release-transaction.json.clearing',
    '.storage-release-transaction.json.recovery',
    '.storage-restore-journal.json',
)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _storage_release_state(runtime: Any) -> str:
    """Return clean only when every durable storage-transaction marker is absent."""
    root = getattr(getattr(runtime, 'store', runtime), 'root', None)
    if root is None:
        return 'unknown'
    state = Path(root) / 'state'
    try:
        for name in _MARKERS:
            path = state / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                return 'unknown'
            return 'in_progress'
    except OSError:
        return 'unknown'
    return 'clean'


def _postgres_projection_applicable(runtime: Any) -> tuple[bool, str]:
    """Only a configured, enabled native PG source needs pre-accept catch-up."""
    try:
        source = runtime.memory.recall_engine.candidate_source
    except Exception:
        return False, 'candidate_source_unavailable'
    from eimemory.retrieval.postgres_vector import PostgresVectorCandidateSource, PostgresVectorConfig
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource

    if type(source) is SQLiteCandidateSource:
        return False, 'sqlite'
    if type(source) is not PostgresVectorCandidateSource:
        return False, 'candidate_source_unsupported'
    config = getattr(source, 'config', None)
    if type(config) is not PostgresVectorConfig or type(config.enabled) is not bool:
        return False, 'candidate_source_config_unavailable'
    if config.enabled is False:
        return False, 'disabled'
    if not config.configured:
        return False, 'not_configured'
    return True, ''


def _maintenance_public(maintenance: dict[str, Any]) -> dict[str, Any]:
    """Copy only secret-free sync diagnostics into the accept report."""
    return {
        key: maintenance[key]
        for key in (
            'ok', 'skipped', 'complete', 'caught_up', 'processed', 'embedded',
            'pages', 'maintenance_mode', 'watermark', 'authority_revision', 'error', 'reason',
        )
        if key in maintenance
    }


def _maintenance_caught_up(maintenance: dict[str, Any]) -> bool:
    if maintenance.get('ok') is not True:
        return False
    skipped = maintenance.get('skipped')
    if skipped in ('disabled', 'already_current'):
        return True
    if maintenance.get('caught_up') is True:
        return True
    if maintenance.get('complete') is True and maintenance.get('error') in (None, ''):
        return True
    return False


def ensure_candidate_projection_restored(
    runtime: Any, *, timeout_seconds: float = DEFAULT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Wait for deploy/storage guard clear, then catch up the projection.

    Elapsed time here is not part of the readiness accept budget. Callers start
    wait_for_candidate_readiness only after this returns.
    """
    started = monotonic()
    result: dict[str, Any] = dict(
        schema=SCHEMA, ok=False, applicable=False, skipped='', blocked_reason='',
        storage_release_state='', maintenance={}, maintenance_rounds=0,
        started_at=_stamp(), completed_at='', elapsed_seconds=0.0,
        timeout_seconds=0.0, wait_seconds=0.0,
    )

    def finish(reason: str = '', *, ok: bool = False, skipped: str = '') -> dict[str, Any]:
        result.update(
            ok=ok, blocked_reason=reason, skipped=skipped, completed_at=_stamp(),
            elapsed_seconds=max(0.0, monotonic() - started),
        )
        return result

    if not _number(timeout_seconds) or timeout_seconds <= 0:
        return finish('projection_restore_timeout_invalid')
    budget = min(float(timeout_seconds), MAX_WAIT_SECONDS)
    result['timeout_seconds'] = budget
    deadline = started + budget

    applicable, skip = _postgres_projection_applicable(runtime)
    if not applicable:
        result['applicable'] = False
        return finish(ok=True, skipped=skip or 'not_applicable')
    result['applicable'] = True

    # Phase 1: do not write the index while a storage-release transaction is open.
    wait_started = monotonic()
    while monotonic() < deadline:
        state = _storage_release_state(runtime)
        result['storage_release_state'] = state
        if state == 'clean':
            break
        if state == 'unknown':
            return finish('storage_release_state_unknown')
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(POLL_SECONDS, remaining))
    result['wait_seconds'] = max(0.0, monotonic() - wait_started)
    if result['storage_release_state'] != 'clean':
        return finish('storage_release_guard_not_clear')

    # Phase 2: in-process catch-up (systemd vector-sync may still be held).
    # maintain_index is bounded (max_pages=8); loop until current or deadline.
    last: dict[str, Any] = {}
    while monotonic() < deadline:
        try:
            from eimemory.retrieval.vector_sync_worker import maintain_index
            maintenance = maintain_index(runtime)
        except Exception:
            return finish('projection_maintenance_unavailable')
        if not isinstance(maintenance, dict):
            return finish('projection_maintenance_invalid')
        result['maintenance_rounds'] += 1
        last = _maintenance_public(maintenance)
        result['maintenance'] = last
        if maintenance.get('ok') is not True:
            return finish(str(maintenance.get('error') or 'projection_maintenance_failed'))
        if _maintenance_caught_up(maintenance):
            return finish(ok=True)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(POLL_SECONDS, remaining))

    if last and last.get('ok') is True:
        return finish('projection_restore_incomplete')
    return finish('projection_restore_deadline_exhausted')
