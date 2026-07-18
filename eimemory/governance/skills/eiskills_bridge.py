"""Minimal in-process bridge for the sibling ``eiskills`` project (Task 4.4).

``eiskills`` is a sibling runtime that hosts its own skill catalog. The real
inter-process integration is out of scope for the Karpathy Loop Phase 4 work,
so this module ships a **stub**: a JSONL-backed manifest store that captures
``register_skill`` / ``deregister_skill`` calls and serves the in-process
queries the rest of the eimemory skill pipeline needs.

The JSONL is append-only: every write becomes a new row. Dedup is done at
read time, so the file doubles as an audit trail. A real eiskills client can
be swapped in later by replacing these four functions with a thin RPC wrapper
— the public surface is intentionally tiny and stable.

Public API:
    - :func:`register_skill`
    - :func:`deregister_skill`
    - :func:`list_active_skills`
    - :func:`get_skill`
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from eimemory.storage.jsonl import JsonlLog, iter_jsonl_payloads


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with timezone info."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: Path) -> None:
    """Create the registry file's parent directory tree if it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _append_row(path: Path, row: dict[str, Any]) -> None:
    """Append a single JSON row to the JSONL registry, creating parents first."""
    _ensure_parent(path)
    JsonlLog(path, max_segment_bytes=16 * 1024 * 1024).append_payload(row)


def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream every registry row without retaining the history in memory.

    A missing file is treated as an empty registry (no rows). Malformed lines
    are skipped silently — the JSONL is best-effort and we never want a single
    bad row to brick the skill pipeline.
    """
    yield from iter_jsonl_payloads(
        path,
        max_row_bytes=16 * 1024 * 1024,
    )


def register_skill(
    *,
    skill_name: str,
    manifest: dict[str, Any],
    version: str,
    registry_path: Path,
) -> None:
    """Record a skill registration in the JSONL manifest store.

    Appends a new row with ``status="active"``. Re-registering the same
    ``(skill_name, version)`` pair produces a second row; readers
    (:func:`list_active_skills`, :func:`get_skill`) collapse the history to
    the latest write, while the raw file keeps the full audit trail.

    Args:
        skill_name: Stable identifier for the skill.
        manifest: Arbitrary JSON-serializable skill manifest (triggers, handler, ...).
        version: Skill version string (e.g. ``"1.0.0"``).
        registry_path: Path to the JSONL registry file. Parent dirs are
            created on demand.
    """
    row = {
        "skill_name": skill_name,
        "version": version,
        "manifest": manifest,
        "status": "active",
        "ts": _now_iso(),
    }
    _append_row(registry_path, row)


def deregister_skill(*, skill_name: str, registry_path: Path) -> None:
    """Record a deregistration event for ``skill_name`` in the JSONL store.

    Appends a new row with ``status="inactive"``. The history is preserved —
    :func:`get_skill` will return the inactive row, while
    :func:`list_active_skills` filters it out.

    Args:
        skill_name: Skill to deregister.
        registry_path: Path to the JSONL registry file.
    """
    # We retain the most recent manifest / version for the deregister row so
    # ``get_skill`` callers can still inspect the skill's last known config.
    last: dict[str, Any] | None = None
    for row in _iter_rows(registry_path):
        if row.get("skill_name") == skill_name:
            last = row
    version = last["version"] if last else ""
    manifest = last["manifest"] if last else {}
    row = {
        "skill_name": skill_name,
        "version": version,
        "manifest": manifest,
        "status": "inactive",
        "ts": _now_iso(),
    }
    _append_row(registry_path, row)


def _latest_per_name(rows: Iterator[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce a row list to the latest row for each ``skill_name`` (by write order)."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("skill_name")
        if not isinstance(name, str):
            continue
        latest[name] = row
    return latest


def list_active_skills(*, registry_path: Path) -> list[dict[str, Any]]:
    """Return all skills whose latest row has ``status="active"``.

    The output preserves the most recent write order; duplicates from prior
    rows are collapsed.

    Args:
        registry_path: Path to the JSONL registry file.

    Returns:
        A list of skill rows, each containing ``skill_name``, ``version``,
        ``manifest``, ``status``, and ``ts``.
    """
    latest = _latest_per_name(_iter_rows(registry_path))
    return [row for row in latest.values() if row.get("status") == "active"]


def get_skill(*, skill_name: str, registry_path: Path) -> dict[str, Any] | None:
    """Return the latest row for ``skill_name`` regardless of status.

    Args:
        skill_name: Skill identifier to look up.
        registry_path: Path to the JSONL registry file.

    Returns:
        The most recent row for ``skill_name``, or ``None`` if it has never
        been written. If the skill has been deregistered, the returned row
        will have ``status="inactive"``.
    """
    latest: dict[str, Any] | None = None
    for row in _iter_rows(registry_path):
        if row.get("skill_name") == skill_name:
            latest = row
    return latest
