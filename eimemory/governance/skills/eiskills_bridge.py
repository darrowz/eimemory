"""Minimal eiskills bridge: JSONL-backed manifest registration.

eiskills is a sibling project — this bridge is a v1 stub that lets eimemory
register, list, look up, and deregister skill manifests without depending on
the eiskills runtime being available. The contract is the in-process manifest
store; the real eiskills integration (likely an HTTP / gRPC call) is deferred
to a future task.

The store is a JSONL file — one row per registration event. Reads collapse
the history into a single "current" row per ``(skill_name, version)`` pair,
keeping the most recent write as the truth. This gives us an audit trail
(every state change is a row) without giving up a clean "list active skills"
view.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


# Default location matches the runtime state convention used by the other
# safety/governance modules. Tests always pass an explicit ``registry_path``,
# so the default is only hit by the production call sites.
DEFAULT_REGISTRY_PATH = Path("/var/lib/eimemory/state/eiskills/registry.jsonl")

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
_VALID_STATUSES = {STATUS_ACTIVE, STATUS_INACTIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_rows(path: Path) -> list[dict]:
    """Read every JSONL row from ``path``; return [] if the file does not exist."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _current_by_skill(rows: list[dict]) -> dict[str, dict]:
    """Reduce the full row history to the most-recent row per skill_name.

    The registry key is ``skill_name`` alone — a skill has at most one
    current state, regardless of how many versions are listed in its
    history. Tests cover the case where the same (skill_name, version) is
    re-registered; the later row wins, the earlier row stays in the file.
    """
    current: dict[str, dict] = {}
    for row in rows:
        name = row.get("skill_name")
        if not isinstance(name, str):
            continue
        current[name] = row
    return current


def register_skill(
    *,
    skill_name: str,
    manifest: dict,
    version: str,
    registry_path: Path | None = None,
) -> dict:
    """Register or update a skill manifest in the JSONL registry.

    Args:
        skill_name: Stable identifier for the skill (e.g. ``"auto-recall"``).
        manifest: Free-form manifest dict (triggers, handler, params, ...).
        version: Skill version string (e.g. ``"1.0.0"``).
        registry_path: Override for the JSONL store. Defaults to
            :data:`DEFAULT_REGISTRY_PATH`. Tests pass an explicit ``tmp_path``.

    Returns:
        The newly written row, including the synthesized ``ts`` and ``status``.
    """
    if not isinstance(manifest, dict):
        raise TypeError(f"manifest must be a dict, got {type(manifest).__name__}")
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    _ensure_parent(path)
    row = {
        "ts": _now_iso(),
        "skill_name": skill_name,
        "version": version,
        "manifest": manifest,
        "status": STATUS_ACTIVE,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    return row


def deregister_skill(
    *,
    skill_name: str,
    registry_path: Path | None = None,
) -> dict | None:
    """Mark a skill as inactive in the registry. Idempotent.

    Args:
        skill_name: The skill to mark inactive.
        registry_path: Override for the JSONL store.

    Returns:
        The new row if the skill existed, else ``None``.
    """
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    rows = _read_rows(path)
    current = _current_by_skill(rows).get(skill_name)
    if current is None:
        return None
    new_row = {
        "ts": _now_iso(),
        "skill_name": skill_name,
        "version": current.get("version", ""),
        "manifest": current.get("manifest", {}),
        "status": STATUS_INACTIVE,
    }
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(new_row, sort_keys=True, ensure_ascii=False) + "\n")
    return new_row


def list_active_skills(registry_path: Path | None = None) -> list[dict]:
    """List every skill currently marked ``status=active``.

    Args:
        registry_path: Override for the JSONL store.

    Returns:
        A list of manifest rows (each with ``skill_name``, ``version``,
        ``manifest``, ``status``, ``ts``). Order matches insertion order in
        the JSONL file (oldest active first).
    """
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    current = _current_by_skill(_read_rows(path))
    return [row for row in current.values() if row.get("status") == STATUS_ACTIVE]


def get_skill(*, skill_name: str, registry_path: Path | None = None) -> dict | None:
    """Return the current row for ``skill_name`` regardless of status, or None."""
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    return _current_by_skill(_read_rows(path)).get(skill_name)
