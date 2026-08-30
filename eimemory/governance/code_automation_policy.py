"""Trusted machine policy for autonomous code evolution.

The policy is intentionally read only from a deployment-controlled environment
variable.  Proposer, incident, candidate, and patch payloads are data and are
never consulted as policy authority.  Returned values are bounded diagnostics
only: the raw environment JSON is never persisted or returned.
"""

from __future__ import annotations

import json
import os
import re
import stat
import errno
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from eimemory.governance.code_patch_command_policy import AUTOMATION_POLICY_ACTIONS


CODE_AUTOMATION_POLICY_ENV = "EIMEMORY_CODE_AUTOMATION_POLICY_JSON"
CODE_AUTOMATION_POLICY_SCHEMA_VERSION = "code_automation_policy.v1"
CODE_AUTOMATION_POLICY_SCHEMA_V2 = "code_automation_policy.v2"
CODE_AUTOMATION_POLICY_PATH_ENV = "EIMEMORY_CODE_AUTOMATION_POLICY_PATH"
CODE_EVOLUTION_KILL_SWITCH_ENV = "EIMEMORY_CODE_EVOLUTION_KILL_SWITCH"
CODE_AUTOMATION_POLICY_DEFAULT_PATH = Path("/etc/eimemory/code-automation-policy.v2.json")
CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH = Path("/etc/eimemory/code-evolution.disabled")
CODE_AUTOMATION_POLICY_SOURCE = "machine_environment"
MAX_POLICY_JSON_CHARS = 16_384
MAX_POLICY_IDENTIFIER_CHARS = 160
MAX_POLICY_COORDINATE_CHARS = 512
MAX_POLICY_CONSTRAINT_VALUES = 128

_POLICY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CONSTRAINT_KEYS = {
    "profile_keys": "profile_key",
    "capability_ids": "capability_id",
    "capability_revision_ids": "capability_revision_id",
    "capability_scopes": "capability_scope",
    "provider_binding_ids": "provider_binding_id",
}


def load_code_automation_policy(
    *,
    profile_key: str = "",
    capability_id: str = "",
    capability_revision_id: str = "",
    capability_scope: str = "",
    provider_binding_id: str = "",
    path: str | os.PathLike[str] | None = None,
    checked_at: str = "",
    kill_switch_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the sole authority for automatic code side effects.

    The returned envelope is safe for proposal, transaction, and diagnostic
    records.  It contains no raw configuration or unknown fields.  A missing,
    malformed or nonmatching policy is a direct machine
    block rather than a deferred or operator-mediated state.
    """
    if path is not None or str(os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or "").strip():
        return _load_v2_policy(
            path=path or os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or CODE_AUTOMATION_POLICY_DEFAULT_PATH,
            checked_at=checked_at,
            kill_switch_path=kill_switch_path,
        )
    context = _policy_context(
        profile_key=profile_key,
        capability_id=capability_id,
        capability_revision_id=capability_revision_id,
        capability_scope=capability_scope,
        provider_binding_id=provider_binding_id,
    )
    raw_text = str(os.environ.get(CODE_AUTOMATION_POLICY_ENV) or "")
    if not raw_text.strip():
        return _blocked_policy("machine_policy_environment_missing", context=context)
    if len(raw_text) > MAX_POLICY_JSON_CHARS:
        return _blocked_policy("machine_policy_environment_too_large", context=context)
    try:
        raw = json.loads(raw_text, object_pairs_hook=_strict_json_object)
    except (json.JSONDecodeError, ValueError):
        return _blocked_policy("machine_policy_environment_invalid_json", context=context)
    if not isinstance(raw, dict):
        return _blocked_policy("machine_policy_environment_not_object", context=context)
    return _validated_policy(raw, context=context)


def machine_policy_context_from_mapping(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Select only stable capability coordinates from untrusted request data.

    This helper does not grant authority.  It merely supplies matching inputs
    for the independent environment policy loader above.
    """
    raw = value if isinstance(value, Mapping) else {}
    hypothesis = raw.get("capability_hypothesis")
    hypothesis = hypothesis if isinstance(hypothesis, Mapping) else {}
    return _policy_context(
        profile_key=_text(raw.get("profile_key")),
        capability_id=_text(raw.get("target_capability") or raw.get("capability_id") or hypothesis.get("capability_id")),
        capability_revision_id=_text(raw.get("capability_revision_id") or hypothesis.get("capability_revision_id")),
        capability_scope=_text(raw.get("capability_scope") or hypothesis.get("capability_scope")),
        provider_binding_id=_text(raw.get("provider_binding_id") or hypothesis.get("provider_binding_id")),
    )


def code_automation_policy_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the fixed safe subset permitted in proposal and ledger records."""
    raw = value if isinstance(value, Mapping) else {}
    raw_actions = raw.get("actions") if isinstance(raw.get("actions"), Mapping) else {}
    actions = {
        action: raw_actions.get(action) is True
        for action in AUTOMATION_POLICY_ACTIONS
    }
    policy_id = _text(raw.get("policy_id"), limit=MAX_POLICY_IDENTIFIER_CHARS)
    digest = str(raw.get("policy_digest") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = ""
    declared = raw.get("declared") is True and bool(policy_id)
    schema_version = (
        CODE_AUTOMATION_POLICY_SCHEMA_VERSION
        if str(raw.get("schema_version") or "") == CODE_AUTOMATION_POLICY_SCHEMA_VERSION
        else ""
    )
    source = (
        CODE_AUTOMATION_POLICY_SOURCE
        if str(raw.get("source") or "") == CODE_AUTOMATION_POLICY_SOURCE
        else ""
    )
    # A summary is often passed through untrusted proposal-shaped payloads.
    # Never let an ``ok: true`` field from such data become authority merely
    # because it resembles a policy receipt.  Only a complete receipt emitted
    # by the environment loader can be enabled; callers that need to apply a
    # change may safely gate on this compact object alone.
    trusted_enabled = (
        raw.get("ok") is True
        and str(raw.get("status") or "") == "enabled"
        and declared
        and bool(schema_version)
        and bool(source)
        and bool(digest)
    )
    reason = _text(raw.get("reason"), limit=160)
    if raw.get("ok") is True and not trusted_enabled and not reason:
        reason = "machine_policy_summary_untrusted"
    return {
        "ok": trusted_enabled,
        "status": "enabled" if trusted_enabled else "blocked",
        "reason": reason,
        "declared": declared,
        "schema_version": schema_version,
        "source": source,
        "policy_id": policy_id,
        "policy_digest": digest,
        "actions": actions,
    }


def _validated_policy(raw: dict[str, Any], *, context: dict[str, str]) -> dict[str, Any]:
    unknown = set(raw) - {"schema_version", "policy_id", "actions", "constraints"}
    if unknown:
        return _blocked_policy("machine_policy_fields_unknown", context=context)
    if str(raw.get("schema_version") or "") != CODE_AUTOMATION_POLICY_SCHEMA_VERSION:
        return _blocked_policy("machine_policy_schema_invalid", context=context)
    if not isinstance(raw.get("policy_id"), str):
        return _blocked_policy("machine_policy_id_invalid", context=context)
    policy_id = _text(raw.get("policy_id"), limit=MAX_POLICY_IDENTIFIER_CHARS)
    if not _POLICY_ID_PATTERN.fullmatch(policy_id):
        return _blocked_policy("machine_policy_id_invalid", context=context)
    actions, action_error = _strict_actions(raw.get("actions"))
    if action_error:
        return _blocked_policy(action_error, context=context, policy_id=policy_id)
    constraints, constraint_error = _strict_constraints(raw.get("constraints"))
    if constraint_error:
        return _blocked_policy(constraint_error, context=context, policy_id=policy_id, actions=actions)
    digest = _policy_digest(raw)
    mismatch_reason = _constraint_mismatch_reason(constraints, context)
    if mismatch_reason:
        return _blocked_policy(
            mismatch_reason,
            context=context,
            policy_id=policy_id,
            policy_digest=digest,
            actions=actions,
            constraints=constraints,
        )
    if not actions["local_apply"]:
        return _blocked_policy(
            "machine_policy_local_apply_not_enabled",
            context=context,
            policy_id=policy_id,
            policy_digest=digest,
            actions=actions,
            constraints=constraints,
        )
    return {
        "ok": True,
        "status": "enabled",
        "reason": "",
        "declared": True,
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
        "source": CODE_AUTOMATION_POLICY_SOURCE,
        "policy_id": policy_id,
        "policy_digest": digest,
        "actions": actions,
        "constraints": constraints,
        "context": context,
    }


def _blocked_policy(
    reason: str,
    *,
    context: dict[str, str],
    policy_id: str = "",
    policy_digest: str = "",
    actions: dict[str, bool] | None = None,
    constraints: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "reason": str(reason),
        "declared": bool(policy_id),
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
        "source": CODE_AUTOMATION_POLICY_SOURCE,
        "policy_id": str(policy_id),
        "policy_digest": str(policy_digest),
        "actions": dict(actions or {action: False for action in AUTOMATION_POLICY_ACTIONS}),
        "constraints": dict(constraints or {}),
        "context": context,
    }


def _strict_actions(value: Any) -> tuple[dict[str, bool], str]:
    if not isinstance(value, dict):
        return {}, "machine_policy_actions_missing"
    names = {str(key) for key in value}
    expected = set(AUTOMATION_POLICY_ACTIONS)
    if names - expected:
        return {}, "machine_policy_actions_unknown"
    if names != expected:
        return {}, "machine_policy_actions_incomplete"
    actions: dict[str, bool] = {}
    for name in AUTOMATION_POLICY_ACTIONS:
        action = value.get(name)
        if not isinstance(action, bool):
            return {}, "machine_policy_actions_invalid"
        actions[name] = action
    return actions, ""


def _strict_constraints(value: Any) -> tuple[dict[str, list[str]], str]:
    if value is None:
        return {}, ""
    if not isinstance(value, dict):
        return {}, "machine_policy_constraints_invalid"
    unknown = {str(key) for key in value} - set(_CONSTRAINT_KEYS)
    if unknown:
        return {}, "machine_policy_constraints_unknown"
    constraints: dict[str, list[str]] = {}
    for key in _CONSTRAINT_KEYS:
        if key not in value:
            continue
        raw_values = value.get(key)
        if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, (list, tuple)):
            return {}, "machine_policy_constraints_invalid"
        if any(not isinstance(item, str) for item in raw_values):
            return {}, "machine_policy_constraints_invalid"
        normalized = [_coordinate(item) for item in raw_values]
        if not normalized or len(normalized) > MAX_POLICY_CONSTRAINT_VALUES:
            return {}, "machine_policy_constraints_invalid"
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            return {}, "machine_policy_constraints_invalid"
        constraints[key] = sorted(normalized)
    return constraints, ""


def _constraint_mismatch_reason(constraints: dict[str, list[str]], context: dict[str, str]) -> str:
    for key, context_key in _CONSTRAINT_KEYS.items():
        allowed = constraints.get(key)
        if not allowed:
            continue
        value = context.get(context_key) or ""
        if not value:
            return f"machine_policy_{context_key}_missing"
        if value not in allowed:
            return f"machine_policy_{context_key}_not_allowed"
    return ""


def _policy_context(
    *,
    profile_key: str,
    capability_id: str,
    capability_revision_id: str,
    capability_scope: str,
    provider_binding_id: str,
) -> dict[str, str]:
    return {
        "profile_key": _coordinate(profile_key),
        "capability_id": _coordinate(capability_id),
        "capability_revision_id": _coordinate(capability_revision_id),
        "capability_scope": _coordinate(capability_scope),
        "provider_binding_id": _coordinate(provider_binding_id),
    }


def _policy_digest(raw: dict[str, Any]) -> str:
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _strict_json_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of accepting a parser overwrite."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate_or_nontext_policy_key")
        result[key] = value
    return result


def _text(value: Any, *, limit: int = MAX_POLICY_IDENTIFIER_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _coordinate(value: Any) -> str:
    """Keep policy matching exact: overlong coordinates never truncate-match."""

    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_POLICY_COORDINATE_CHARS else ""


# ---------------------------------------------------------------------------
# v2 transaction policy

_V2_TOP_LEVEL = {
    "schema_version",
    "policy_id",
    "not_before",
    "expires_at",
    "max_transactions",
    "incident",
    "capability",
    "repository",
    "patch",
    "verification",
    "effects",
    "deployment",
}
_V2_INCIDENT = {"class", "detector_id", "incident_digest"}
_V2_CAPABILITY = {
    "profile_key",
    "capability_id",
    "revision_id",
    "binding_id",
    "implementation_digest",
    "operation",
}
_V2_REPOSITORY = {"root", "remote", "remote_url_digest", "branch", "base_commit", "base_tree_digest"}
_V2_PATCH = {
    "allowed_files",
    "max_files",
    "max_file_bytes",
    "max_total_bytes",
    "max_changed_lines",
    "max_diff_bytes",
}
_V2_VERIFICATION = {"test_plan_id", "test_plan_digest", "full_suite_required"}
_V2_EFFECTS = {"commit", "push", "deployment", "rollback", "sedimentation"}
_V2_DEPLOYMENT = {"installer_digest", "current_link", "health_url", "observation_seconds"}
_V2_ALLOWED_FILES = {
    "deploy/install_immutable_release.sh",
    "deploy/runtime_identity_policy.py",
    "eimemory/governance/l5_reader.py",
    "eimemory/governance/release_closure.py",
    "eimemory/governance/release_closure_gate_evidence.py",
    "eimemory/governance/release_closure_lineage.py",
    "eimemory/governance/release_lineage.py",
    "eimemory/ops/release_closure_failure.py",
    "tests/test_runtime_identity_policy.py",
}
_V2_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_V2_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _v2_block(reason: str, *, path: str = "", policy_id: str = "", policy_digest: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_V2,
        "source": "machine_file",
        "reason": reason,
        "policy_path": path,
        "policy_id": policy_id,
        "policy_digest": policy_digest,
        "effects": {key: False for key in sorted(_V2_EFFECTS)},
        "declared": bool(policy_id),
    }


def _v2_exact(value: Any, expected: set[str], *, field: str) -> str:
    if not isinstance(value, dict):
        return f"{field}_invalid"
    unknown = set(value).difference(expected)
    if unknown:
        return "policy_fields_unknown" if field == "policy" else f"{field}_unknown_fields"
    required = expected if field != "incident" else {"class", "detector_id"}
    missing = required.difference(value)
    if missing:
        return f"{field}_missing_fields"
    return ""


def _v2_sha(value: Any, *, field: str) -> str:
    return "" if isinstance(value, str) and _V2_HEX64_RE.fullmatch(value) else f"{field}_invalid"


def _v2_timestamp(value: Any, *, field: str) -> tuple[datetime | None, str]:
    if not isinstance(value, str) or not value:
        return None, f"{field}_invalid"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, f"{field}_invalid"
    if parsed.tzinfo is None:
        return None, f"{field}_invalid"
    return parsed.astimezone(timezone.utc), ""


def _v2_no_symlink(path: Path, *, missing_ok: bool) -> str:
    absolute = path.absolute()
    for component in (absolute.parent, *absolute.parent.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return "policy_file_unreadable"
        if stat.S_ISLNK(metadata.st_mode):
            return "policy_symlink_rejected"
        if component == Path(absolute.anchor):
            break
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return "" if missing_ok else "policy_file_missing"
    except OSError:
        return "policy_file_unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        return "policy_symlink_rejected"
    if not stat.S_ISREG(metadata.st_mode):
        return "policy_file_not_regular"
    return ""


def _secure_read_v2_policy(path: Path) -> tuple[str, str]:
    """Read one owner-only policy through a no-follow file descriptor."""

    path_error = _v2_no_symlink(path, missing_ok=False)
    if path_error:
        return "", path_error
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name == "posix" and not nofollow:
        return "", "policy_secure_open_unavailable"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return "", "policy_symlink_rejected"
        return "", "policy_file_unreadable"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return "", "policy_file_not_regular"
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            return "", "policy_permissions_invalid"
        if metadata.st_uid != os.geteuid():
            return "", "policy_owner_invalid"
        chunks: list[bytes] = []
        remaining = 64 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 64 * 1024:
            return "", "policy_file_too_large"
        try:
            return raw.decode("utf-8"), ""
        except UnicodeError:
            return "", "policy_file_unreadable"
    finally:
        os.close(descriptor)


def _load_v2_policy(*, path: str | os.PathLike[str], checked_at: str, kill_switch_path: str | os.PathLike[str] | None) -> dict[str, Any]:
    policy_path = Path(path)
    raw_text, path_error = _secure_read_v2_policy(policy_path)
    if path_error:
        return _v2_block(path_error, path=str(policy_path))
    try:
        raw = json.loads(raw_text, object_pairs_hook=_strict_json_object)
    except (json.JSONDecodeError, ValueError):
        return _v2_block("policy_json_invalid", path=str(policy_path))
    if not isinstance(raw, dict):
        return _v2_block("policy_not_object", path=str(policy_path))
    exact_error = _v2_exact(raw, _V2_TOP_LEVEL, field="policy")
    if exact_error:
        return _v2_block(exact_error, path=str(policy_path))
    if raw.get("schema_version") != CODE_AUTOMATION_POLICY_SCHEMA_V2:
        return _v2_block("policy_schema_invalid", path=str(policy_path))
    policy_id = raw.get("policy_id")
    if not isinstance(policy_id, str) or not _POLICY_ID_PATTERN.fullmatch(policy_id):
        return _v2_block("policy_id_invalid", path=str(policy_path))
    not_before, error = _v2_timestamp(raw.get("not_before"), field="policy_not_before")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    expires_at, error = _v2_timestamp(raw.get("expires_at"), field="policy_expires_at")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    if expires_at <= not_before:
        return _v2_block("policy_time_window_invalid", path=str(policy_path), policy_id=policy_id)
    max_transactions = raw.get("max_transactions")
    if isinstance(max_transactions, bool) or max_transactions != 1:
        return _v2_block("policy_max_transactions_invalid", path=str(policy_path), policy_id=policy_id)
    incident = raw.get("incident")
    error = _v2_exact(incident, _V2_INCIDENT, field="incident")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    if not all(isinstance(incident.get(key), str) and incident.get(key) for key in ("class", "detector_id")):
        return _v2_block("incident_coordinates_invalid", path=str(policy_path), policy_id=policy_id)
    if incident.get("incident_digest") is not None and _v2_sha(incident.get("incident_digest"), field="incident_digest"):
        return _v2_block("incident_digest_invalid", path=str(policy_path), policy_id=policy_id)
    capability = raw.get("capability")
    error = _v2_exact(capability, _V2_CAPABILITY, field="capability")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    for key in _V2_CAPABILITY:
        if not isinstance(capability.get(key), str) or not capability.get(key):
            return _v2_block("capability_coordinates_invalid", path=str(policy_path), policy_id=policy_id)
    if _v2_sha(capability.get("implementation_digest"), field="implementation_digest"):
        return _v2_block("implementation_digest_invalid", path=str(policy_path), policy_id=policy_id)
    repository = raw.get("repository")
    error = _v2_exact(repository, _V2_REPOSITORY, field="repository")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    if repository.get("root") != "/dev-project/eimemory" or repository.get("remote") != "origin" or repository.get("branch") != "master":
        return _v2_block("repository_coordinates_invalid", path=str(policy_path), policy_id=policy_id)
    if _v2_sha(repository.get("remote_url_digest"), field="remote_url_digest") or _v2_sha(repository.get("base_tree_digest"), field="base_tree_digest"):
        return _v2_block("repository_digest_invalid", path=str(policy_path), policy_id=policy_id)
    if not isinstance(repository.get("base_commit"), str) or not _V2_COMMIT_RE.fullmatch(repository["base_commit"]):
        return _v2_block("base_commit_invalid", path=str(policy_path), policy_id=policy_id)
    patch = raw.get("patch")
    error = _v2_exact(patch, _V2_PATCH, field="patch")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    allowed_files = patch.get("allowed_files")
    if not isinstance(allowed_files, list) or not allowed_files or len(allowed_files) > 4 or any(not isinstance(item, str) for item in allowed_files):
        return _v2_block("patch_allowed_files_invalid", path=str(policy_path), policy_id=policy_id)
    normalized_files: list[str] = []
    for item in allowed_files:
        value = item.replace("\\", "/")
        if value not in _V2_ALLOWED_FILES or value in normalized_files:
            return _v2_block("patch_allowed_files_not_protected", path=str(policy_path), policy_id=policy_id)
        normalized_files.append(value)
    for key in ("max_files", "max_file_bytes", "max_total_bytes", "max_changed_lines", "max_diff_bytes"):
        value = patch.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return _v2_block("patch_bounds_invalid", path=str(policy_path), policy_id=policy_id)
    if patch["max_files"] > len(normalized_files) or patch["max_file_bytes"] > 49_152 or patch["max_total_bytes"] > 96 * 1024 or patch["max_changed_lines"] > 400 or patch["max_diff_bytes"] > 256 * 1024:
        return _v2_block("patch_bounds_exceeded", path=str(policy_path), policy_id=policy_id)
    verification = raw.get("verification")
    error = _v2_exact(verification, _V2_VERIFICATION, field="verification")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    if not isinstance(verification.get("test_plan_id"), str) or not verification["test_plan_id"] or _v2_sha(verification.get("test_plan_digest"), field="test_plan_digest") or verification.get("full_suite_required") is not True:
        return _v2_block("verification_coordinates_invalid", path=str(policy_path), policy_id=policy_id)
    effects = raw.get("effects")
    error = _v2_exact(effects, _V2_EFFECTS, field="effects")
    if error or any(not isinstance(effects.get(key), bool) for key in _V2_EFFECTS):
        return _v2_block(error or "effects_invalid", path=str(policy_path), policy_id=policy_id)
    deployment = raw.get("deployment")
    error = _v2_exact(deployment, _V2_DEPLOYMENT, field="deployment")
    if error:
        return _v2_block(error, path=str(policy_path), policy_id=policy_id)
    if _v2_sha(deployment.get("installer_digest"), field="installer_digest") or deployment.get("current_link") != "/opt/eimemory/current" or deployment.get("health_url") != "http://127.0.0.1:8091/health" or deployment.get("observation_seconds") != 172_800:
        return _v2_block("deployment_coordinates_invalid", path=str(policy_path), policy_id=policy_id)
    reference_text = checked_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    reference, error = _v2_timestamp(reference_text, field="checked_at")
    if error or not (not_before <= reference < expires_at):
        return _v2_block("policy_expired_or_not_yet_valid", path=str(policy_path), policy_id=policy_id)
    switch = Path(
        kill_switch_path
        or os.environ.get(CODE_EVOLUTION_KILL_SWITCH_ENV)
        or CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH
    )
    switch_error = _v2_no_symlink(switch, missing_ok=True)
    if switch_error:
        return _v2_block(switch_error, path=str(policy_path), policy_id=policy_id)
    if switch.exists():
        return _v2_block("kill_switch_present", path=str(policy_path), policy_id=policy_id)
    policy_digest = _policy_digest(raw)
    return {
        "ok": True,
        "status": "enabled",
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_V2,
        "source": "machine_file",
        "reason": "",
        "declared": True,
        "policy_path": str(policy_path),
        "policy_id": policy_id,
        "policy_digest": policy_digest,
        "checked_at": reference.isoformat().replace("+00:00", "Z"),
        "not_before": raw["not_before"],
        "expires_at": raw["expires_at"],
        "incident": dict(incident),
        "capability": dict(capability),
        "repository": dict(repository),
        "patch": {**dict(patch), "allowed_files": normalized_files},
        "verification": dict(verification),
        "effects": dict(effects),
        "deployment": dict(deployment),
        "kill_switch_path": str(switch),
    }


def _transaction_policy_mismatch(
    policy: Mapping[str, Any],
    transaction: Mapping[str, Any],
) -> str:
    payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
    proposal = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else payload
    provider = proposal.get("provider") if isinstance(proposal.get("provider"), Mapping) else (
        payload.get("provider") if isinstance(payload.get("provider"), Mapping) else {}
    )
    repository = proposal.get("repository") if isinstance(proposal.get("repository"), Mapping) else (
        payload.get("repository") if isinstance(payload.get("repository"), Mapping) else {}
    )
    incident = policy.get("incident") if isinstance(policy.get("incident"), Mapping) else {}
    capability = policy.get("capability") if isinstance(policy.get("capability"), Mapping) else {}
    expected = (
        ("incident_class", incident.get("class"), transaction.get("incident_class")),
        ("detector", incident.get("detector_id"), transaction.get("detector")),
        ("profile_key", capability.get("profile_key"), proposal.get("profile_key") or payload.get("profile_key")),
        ("capability_id", capability.get("capability_id"), transaction.get("capability_id")),
        ("revision_id", capability.get("revision_id"), transaction.get("revision_id")),
        ("binding_id", capability.get("binding_id"), transaction.get("binding_id")),
        ("implementation_digest", capability.get("implementation_digest"), transaction.get("implementation_digest")),
        ("operation", capability.get("operation"), provider.get("operation")),
        ("repository_root", policy.get("repository", {}).get("root"), transaction.get("repository_root")),
        ("repository_remote", policy.get("repository", {}).get("remote"), transaction.get("repository_remote")),
        ("repository_ref", policy.get("repository", {}).get("branch"), str(transaction.get("repository_ref") or "").removeprefix("refs/heads/")),
        ("base_commit", policy.get("repository", {}).get("base_commit"), transaction.get("base_commit")),
        ("base_tree_digest", policy.get("repository", {}).get("base_tree_digest"), transaction.get("base_tree_digest")),
        ("remote_url_digest", policy.get("repository", {}).get("remote_url_digest"), repository.get("remote_url_digest")),
    )
    for field, wanted, observed in expected:
        if str(wanted or "") != str(observed or ""):
            return f"policy_transaction_{field}_mismatch"
    required_incident_digest = str(incident.get("incident_digest") or "")
    if required_incident_digest and required_incident_digest != str(transaction.get("incident_digest") or ""):
        return "policy_transaction_incident_digest_mismatch"
    updates = proposal.get("file_updates")
    if not isinstance(updates, list) or not updates:
        return "policy_transaction_patch_files_missing"
    actual_files = {
        str(item.get("path") or "")
        for item in updates
        if isinstance(item, Mapping) and str(item.get("path") or "")
    }
    allowed_files = set(str(item) for item in policy.get("patch", {}).get("allowed_files") or ())
    if not actual_files or not actual_files.issubset(allowed_files):
        return "policy_transaction_patch_files_mismatch"
    for field in ("proposal_digest", "patch_digest", "candidate_tree_digest"):
        if not _V2_HEX64_RE.fullmatch(str(transaction.get(field) or "")):
            return f"policy_transaction_{field}_missing"
    for field in ("advertisement_digest", "catalog_snapshot_digest"):
        if not _V2_HEX64_RE.fullmatch(str(transaction.get(field) or "")):
            return f"policy_transaction_{field}_missing"
    if not str(transaction.get("advertisement_id") or ""):
        return "policy_transaction_advertisement_id_missing"
    if not str(transaction.get("catalog_case_id") or ""):
        return "policy_transaction_catalog_case_id_missing"
    return ""


def consume_code_automation_policy(
    *,
    path: str | os.PathLike[str],
    transaction_id: str,
    expected_digest: str,
    store: Any,
) -> dict[str, Any]:
    """Atomically consume the one-shot v2 policy through SQLite authority."""

    if store is None:
        return {"ok": False, "reason": "policy_store_required", "idempotent": False}
    policy = load_code_automation_policy(path=path)
    if policy.get("ok") is not True:
        return {"ok": False, "reason": str(policy.get("reason") or "policy_blocked"), "idempotent": False}
    if str(policy.get("policy_digest") or "") != str(expected_digest or ""):
        return {"ok": False, "reason": "policy_digest_mismatch", "idempotent": False}
    from eimemory.storage.code_evolution_store import CodeEvolutionStore, canonical_json

    try:
        ledger = CodeEvolutionStore(store)
        transaction = ledger.get_transaction(str(transaction_id))
        if transaction is None:
            return {"ok": False, "reason": "policy_transaction_not_found", "idempotent": False}
        mismatch = _transaction_policy_mismatch(policy, transaction)
        if mismatch:
            return {"ok": False, "reason": mismatch, "idempotent": False}
        verification = ledger.list_verification_receipts(str(transaction_id))
        required_kinds = {"focused", "regression", "full_suite"}
        by_kind = {
            str(receipt.get("verification_kind") or ""): receipt
            for receipt in verification
        }
        if set(by_kind) != required_kinds:
            return {"ok": False, "reason": "policy_verification_receipts_incomplete", "idempotent": False}
        for kind in required_kinds:
            receipt = by_kind[kind]
            if (
                receipt.get("result") != "pass"
                or int(receipt.get("exit_status", 1)) != 0
                or receipt.get("base_commit") != transaction.get("base_commit")
                or receipt.get("patch_digest") != transaction.get("patch_digest")
                or receipt.get("candidate_tree_digest") != transaction.get("candidate_tree_digest")
                or receipt.get("test_plan_id") != policy["verification"]["test_plan_id"]
                or receipt.get("test_plan_digest") != policy["verification"]["test_plan_digest"]
                or not _V2_HEX64_RE.fullmatch(str(receipt.get("receipt_digest") or ""))
            ):
                return {"ok": False, "reason": f"policy_{kind}_verification_invalid", "idempotent": False}
        authorization_material = {
            "schema": "code_automation_authorization.v2",
            "transaction_id": str(transaction_id),
            "policy_digest": str(policy["policy_digest"]),
            "incident_digest": str(transaction.get("incident_digest") or ""),
            "proposal_digest": str(transaction.get("proposal_digest") or ""),
            "patch_digest": str(transaction.get("patch_digest") or ""),
            "candidate_tree_digest": str(transaction.get("candidate_tree_digest") or ""),
            "provider": {
                field: transaction.get(field)
                for field in (
                    "capability_id",
                    "revision_id",
                    "binding_id",
                    "provider_kind",
                    "provider_instance_id",
                    "implementation_digest",
                )
            },
            "advertisement": {
                "advertisement_id": transaction.get("advertisement_id"),
                "advertisement_digest": transaction.get("advertisement_digest"),
            },
            "catalog": {
                "catalog_case_id": transaction.get("catalog_case_id"),
                "catalog_snapshot_digest": transaction.get("catalog_snapshot_digest"),
            },
            "repository": {
                field: transaction.get(field)
                for field in (
                    "repository_root",
                    "repository_remote",
                    "repository_ref",
                    "base_commit",
                    "base_tree_digest",
                )
            },
            "verification_receipt_digests": sorted(
                str(receipt["receipt_digest"]) for receipt in verification
            ),
            "effects": dict(policy["effects"]),
            "deployment": dict(policy["deployment"]),
        }
        authorization_digest = sha256(
            canonical_json(authorization_material).encode("utf-8")
        ).hexdigest()
        result = ledger.consume_policy(
            policy_digest=str(policy["policy_digest"]),
            transaction_id=str(transaction_id),
            authorization_receipt_digest=authorization_digest,
            payload={
                "policy_id": policy.get("policy_id"),
                "policy_path": policy.get("policy_path"),
                "authorization_material": authorization_material,
                "authorized_policy": {
                    key: value for key, value in policy.items() if key != "checked_at"
                },
            },
        )
    except Exception as exc:
        if type(exc).__name__ == "CodeEvolutionConflict":
            return {"ok": False, "reason": "policy_already_consumed", "idempotent": False}
        return {"ok": False, "reason": "policy_consumption_failed", "idempotent": False}
    return {"ok": True, **result}


__all__ = [
    "CODE_AUTOMATION_POLICY_ENV",
    "CODE_AUTOMATION_POLICY_DEFAULT_PATH",
    "CODE_AUTOMATION_POLICY_PATH_ENV",
    "CODE_AUTOMATION_POLICY_SCHEMA_VERSION",
    "CODE_AUTOMATION_POLICY_SCHEMA_V2",
    "CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH",
    "CODE_EVOLUTION_KILL_SWITCH_ENV",
    "CODE_AUTOMATION_POLICY_SOURCE",
    "code_automation_policy_summary",
    "load_code_automation_policy",
    "consume_code_automation_policy",
    "machine_policy_context_from_mapping",
]
