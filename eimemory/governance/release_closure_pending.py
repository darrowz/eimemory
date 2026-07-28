from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    same_release_authority,
)
from eimemory.storage.atomic_file import read_json_strict


SCHEMA_VERSION = "release_closure_pending.v1"
WAITING_STATUS = "waiting_for_channel_acceptance"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_KEYS = frozenset({"version", "release_version", "deployment_version"})


def default_release_closure_pending_path() -> Path | None:
    configured = str(os.environ.get("EIMEMORY_RELEASE_CLOSURE_PENDING_PATH") or "").strip()
    if configured:
        return Path(configured)
    root = str(os.environ.get("EIMEMORY_ROOT") or "").strip()
    return Path(root) / "state" / "release-closure-pending.json" if root else None


def build_release_closure_pending(
    *,
    scope: Mapping[str, Any],
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
    current_release: ReleaseIdentity,
    release_path: str,
    record_ids: Mapping[str, Any],
    replay_bootstrap: Mapping[str, Any],
    live_acceptance: Mapping[str, Any],
    bootstrap_pending: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": WAITING_STATUS,
        "current_commit": str(current_release.commit or "").strip().lower(),
        "prior_commit": str(prior_commit or "").strip().lower(),
        "deployment_receipt_id": str(current_release.receipt_id or "").strip(),
        "release_session_id": str(current_release.session_id or "").strip(),
        "release_path": str(release_path or "").strip(),
        "scope": _json_object(scope),
        "inputs": {
            "repo_root": str(repo_root),
            "current_link": str(current_link),
            "health_url": str(health_url),
        },
        "passed_gate_record_ids": _json_object(record_ids),
        "passed_gate_reports": {
            "replay_bootstrap": _strip_version_metadata(replay_bootstrap),
            "live_acceptance": _strip_version_metadata(live_acceptance),
            "bootstrap_pending": _strip_version_metadata(bootstrap_pending or {}),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def write_release_closure_pending(
    checkpoint: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    target = _resolve_path(path)
    if target is None:
        return {"ok": False, "status": "disabled", "error": "pending_path_unconfigured"}
    normalized = _validate_checkpoint(dict(checkpoint))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "status": WAITING_STATUS, "path": str(target)}


def read_release_closure_pending(
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    target = _resolve_path(path)
    if target is None or not target.exists():
        return None
    return _validate_checkpoint(read_json_strict(target, dict))


def clear_release_closure_pending(
    *,
    path: str | Path | None = None,
    expected_commit: str = "",
) -> bool:
    target = _resolve_path(path)
    if target is None or not target.exists():
        return False
    checkpoint = read_release_closure_pending(path=target)
    if checkpoint is None:
        return False
    expected = str(expected_commit or "").strip().lower()
    if expected and checkpoint["current_commit"] != expected:
        return False
    target.unlink()
    _fsync_directory(target.parent)
    return True


def reconcile_release_closure_pending(
    runtime: Any,
    *,
    pending_path: str | Path | None = None,
) -> dict[str, Any]:
    try:
        checkpoint = read_release_closure_pending(path=pending_path)
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "status": "invalid",
            "error": "release_closure_pending_invalid",
            "detail": str(exc),
        }
    if checkpoint is None:
        return {"ok": True, "status": "no_pending"}
    scope = checkpoint["scope"]
    current = runtime.current_release_identity(scope=scope, limit=500)
    expected = ReleaseIdentity(
        commit=checkpoint["current_commit"],
        version="",
        receipt_id=checkpoint["deployment_receipt_id"],
        session_id=checkpoint["release_session_id"],
    )
    if not same_release_authority(current, expected):
        return {
            "ok": False,
            "status": "stale",
            "error": "pending_release_authority_mismatch",
        }
    channel_acceptance = runtime.record_openclaw_channel_acceptance(
        scope=scope,
        current_release=current,
    )
    if channel_acceptance.get("ok") is not True:
        reason = str(
            channel_acceptance.get("error")
            or channel_acceptance.get("reason")
            or "current_release_channel_acceptance_missing"
        )
        if reason == "current_release_channel_receipt_not_found":
            return {
                "ok": True,
                "status": WAITING_STATUS,
                "reason": reason,
            }
        return {
            "ok": False,
            "status": "blocked",
            "error": reason,
        }
    from eimemory.governance.release_closure import resume_release_closure

    report = resume_release_closure(
        runtime,
        checkpoint=checkpoint,
        current_release=current,
        channel_acceptance=channel_acceptance,
    )
    if report.get("ok") is True:
        clear_release_closure_pending(
            path=pending_path,
            expected_commit=current.commit,
        )
    return report


def _validate_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    if (
        checkpoint.get("schema_version") != SCHEMA_VERSION
        or checkpoint.get("status") != WAITING_STATUS
    ):
        raise ValueError("release closure pending contract mismatch")
    for key in ("current_commit", "prior_commit"):
        value = str(checkpoint.get(key) or "").strip().lower()
        if _COMMIT_RE.fullmatch(value) is None:
            raise ValueError(f"{key} must be a full lowercase commit")
        checkpoint[key] = value
    for key in ("deployment_receipt_id", "release_session_id", "release_path"):
        value = str(checkpoint.get(key) or "").strip()
        if not value:
            raise ValueError(f"{key} must be nonempty")
        checkpoint[key] = value
    checkpoint["scope"] = _required_object(checkpoint, "scope")
    checkpoint["inputs"] = _required_object(checkpoint, "inputs")
    checkpoint["passed_gate_record_ids"] = _required_object(
        checkpoint, "passed_gate_record_ids"
    )
    reports = _required_object(checkpoint, "passed_gate_reports")
    for key in ("replay_bootstrap", "live_acceptance", "bootstrap_pending"):
        reports[key] = _required_object(reports, key)
    checkpoint["passed_gate_reports"] = reports
    if not str(checkpoint.get("created_at") or "").strip():
        raise ValueError("created_at must be nonempty")
    return checkpoint


def _required_object(parent: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


def _strip_version_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_version_metadata(item)
            for key, item in value.items()
            if str(key) not in _VERSION_KEYS
        }
    if isinstance(value, list):
        return [_strip_version_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_version_metadata(item) for item in value]
    return value


def _resolve_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    return default_release_closure_pending_path()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
