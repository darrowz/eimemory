"""Issue the next-round code-automation-policy.v2 from live repository state.

Safe defaults: effects are all-disabled unless an explicit mode is chosen.
This module never writes /etc/eimemory unless ``install_path`` is provided.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
import json
import os
import re
import subprocess
import stat
import tempfile
import time

from eimemory.governance.evolution.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    CODE_AUTOMATION_POLICY_PATH_ENV,
    CODE_AUTOMATION_POLICY_SCHEMA_V2,
)
from eimemory.governance.evolution.code_evolution_observation import DEFAULT_OBSERVATION_SECONDS
from eimemory.governance.evolution.code_evolution_repository import protected_paths_digest, remote_url_digest
from eimemory.governance.evolution.code_evolution_path_policy import (
    DEFAULT_ALLOWED_PATH_GLOBS,
    DEFAULT_DENIED_PATH_GLOBS,
)
from eimemory.governance.evolution.code_evolution_test_plans import (
    allowed_files_for_incident,
    protected_test_plan,
    protected_test_plan_digest,
)


EffectsMode = Literal["all-disabled", "commit-push-only", "full"]
AUTO_ISSUE_ENV = "EIMEMORY_CODE_EVOLUTION_AUTO_ISSUE"

_EFFECTS_BY_MODE: dict[str, dict[str, bool]] = {
    "all-disabled": {
        "commit": False,
        "push": False,
        "deployment": False,
        "rollback": False,
        "sedimentation": False,
    },
    "commit-push-only": {
        "commit": True,
        "push": True,
        "deployment": False,
        "rollback": False,
        "sedimentation": False,
    },
    "full": {
        "commit": True,
        "push": True,
        "deployment": True,
        "rollback": True,
        "sedimentation": True,
    },
}


def issue_code_automation_policy(
    *,
    repo_root: str | Path | None = None,
    incident_class: str,
    detector_id: str,
    test_plan_id: str = "",
    allowed_files: Sequence[str] | None = None,
    effects_mode: EffectsMode = "all-disabled",
    policy_id: str = "",
    profile_key: str = "l5.default",
    not_before: str | None = None,
    expires_at: str | None = None,
    max_transactions: int = 1,
    install_path: str | Path | None = None,
    incident_digest: str | None = None,
) -> dict[str, Any]:
    """Build a next-round v2 policy filled from HEAD digests.

    Returns ``{"ok": True, "policy": {...}, "written_path": ...}`` or a blocked
    diagnostic. Writing occurs only when ``install_path`` is set (CLI
    ``--install-path``) or when callers pass an explicit destination.
    """

    mode = str(effects_mode or "all-disabled")
    if mode not in _EFFECTS_BY_MODE:
        return {"ok": False, "reason": "effects_mode_invalid", "policy": {}}
    if isinstance(max_transactions, bool) or not isinstance(max_transactions, int) or not (1 <= max_transactions <= 8):
        return {"ok": False, "reason": "policy_max_transactions_invalid", "policy": {}}
    if incident_digest is not None and (
        not isinstance(incident_digest, str) or re.fullmatch(r"[0-9a-f]{64}", incident_digest) is None
    ):
        return {"ok": False, "reason": "incident_digest_invalid", "policy": {}}

    root = Path(repo_root or _default_repo_root()).expanduser().resolve()
    if not (root / ".git").exists() and not (root / ".git").is_file():
        return {"ok": False, "reason": "repository_root_not_git", "policy": {}, "repository_root": str(root)}

    from eimemory.adapters.hermes import code_implementation as provider
    from eimemory.governance.release.deployment_receipt import (
        DEFAULT_DEPLOYMENT_CURRENT_LINK,
        DEFAULT_DEPLOYMENT_HEALTH_URL,
    )

    plan_id = str(test_plan_id or "").strip()
    if not plan_id:
        # Best-effort: first matching protected plan for the incident.
        for candidate in (
            "l5.product-completion-reporting.v1",
            "deployment.runtime-identity-drift.v1",
            "release.closure-self-repair.v1",
            "release.closure-report-repair.v1",
            "code.incident-routing-repair.v1",
        ):
            files = allowed_files_for_incident(incident_class, test_plan_id=candidate)
            if files:
                plan_id = candidate
                break
    plan = protected_test_plan(plan_id)
    if plan is None:
        return {"ok": False, "reason": "test_plan_not_registered", "policy": {}}

    files = tuple(str(item).replace("\\", "/") for item in (allowed_files or plan.allowed_files))
    if not files:
        return {"ok": False, "reason": "allowed_files_empty", "policy": {}}
    expected = allowed_files_for_incident(incident_class, test_plan_id=plan_id)
    if expected and files != expected:
        # Prefer the protected plan intersection when the incident is known.
        files = expected

    try:
        from eimemory.config.trusted import trusted_remote, trusted_repository_root

        repository_root = str(trusted_repository_root())
        remote_name = trusted_remote()
    except Exception:
        repository_root = str(root)
        remote_name = "origin"

    try:
        if _git(root, "status", "--porcelain"):
            return {"ok": False, "reason": "repository_not_clean", "policy": {}}
        base_commit = _git(root, "rev-parse", "HEAD")
        branch = _git(root, "symbolic-ref", "--short", "HEAD")
        remote_url = _git(root, "remote", "get-url", remote_name)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"ok": False, "reason": f"git_identity_unavailable:{type(exc).__name__}", "policy": {}}

    installer = root / "deploy" / "install_immutable_release.sh"
    if not installer.is_file():
        return {"ok": False, "reason": "installer_missing", "policy": {}}

    implementation_digest = str(getattr(provider, "IMPLEMENTATION_DIGEST", "") or "")
    if len(implementation_digest) != 64:
        # Fall back to hashing the provider module for bootstrap examples.
        implementation_digest = sha256(Path(provider.__file__).read_bytes()).hexdigest()

    now = datetime.now(timezone.utc)
    not_before_value = not_before or now.isoformat(timespec="seconds").replace("+00:00", "Z")
    expires_value = expires_at or (now + timedelta(days=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    issued_policy_id = policy_id or f"issued-{incident_class.replace('.', '-')}-{base_commit[:12]}"

    incident_block: dict[str, Any] = {
        "class": str(incident_class),
        "detector_id": str(detector_id),
    }
    if incident_digest:
        incident_block["incident_digest"] = str(incident_digest)

    policy = {
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_V2,
        "policy_id": issued_policy_id,
        "not_before": not_before_value,
        "expires_at": expires_value,
        "max_transactions": int(max_transactions),
        "incident": incident_block,
        "capability": {
            "profile_key": str(profile_key or "l5.default"),
            "capability_id": provider.CAPABILITY_ID,
            "revision_id": provider.REVISION_ID,
            "binding_id": provider.BINDING_ID,
            "implementation_digest": implementation_digest,
            "operation": provider.OPERATION,
        },
        "repository": {
            "root": repository_root,
            "remote": remote_name,
            "remote_url_digest": remote_url_digest(remote_url),
            "branch": branch,
            "base_commit": base_commit,
            "base_tree_digest": protected_paths_digest(root, files),
        },
        "patch": {
            "allowed_files": list(files),
            "allowed_path_globs": list(plan.allowed_path_globs or ()),
            "denied_path_globs": list(DEFAULT_DENIED_PATH_GLOBS),
            "max_files": min(4, len(files) or 4),
            "max_file_bytes": 49_152,
            "max_total_bytes": min(96 * 1024, 49_152 * max(len(files), 1)),
            "max_changed_lines": 400,
            "max_diff_bytes": 262_144,
        },
        "verification": {
            "test_plan_id": plan_id,
            "test_plan_digest": protected_test_plan_digest(plan_id),
            "full_suite_required": True,
        },
        "effects": dict(_EFFECTS_BY_MODE[mode]),
        "deployment": {
            "installer_digest": sha256(installer.read_bytes()).hexdigest(),
            "current_link": str(DEFAULT_DEPLOYMENT_CURRENT_LINK),
            "health_url": str(DEFAULT_DEPLOYMENT_HEALTH_URL),
            "observation_seconds": int(DEFAULT_OBSERVATION_SECONDS),
        },
    }

    written_path = ""
    if install_path is not None:
        destination = Path(install_path)
        try:
            error = _install_policy_atomically(destination, policy)
        except OSError as exc:
            error = f"policy_install_error:{type(exc).__name__}"
        if error:
            return {"ok": False, "reason": error, "policy": {}, "written_path": ""}
        written_path = str(destination)

    return {
        "ok": True,
        "reason": "",
        "effects_mode": mode,
        "policy": policy,
        "written_path": written_path,
        "observation_seconds": int(DEFAULT_OBSERVATION_SECONDS),
        "revision_id": provider.REVISION_ID,
        "binding_id": provider.BINDING_ID,
        "system_route_policy_bound": incident_digest is not None,
    }


@contextmanager
def _policy_lock(path: Path):
    """Serialize native writers without following an authority path link."""
    from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
    if _v2_no_symlink(path, missing_ok=True):
        raise ValueError("policy_lock_path_invalid")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), 0o600)
    acquired = False
    try:
        info = os.fstat(descriptor)
        named = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or (os.name == "posix" and (info.st_uid != os.geteuid()
                                             or stat.S_IMODE(info.st_mode) & 0o077))):
            raise ValueError("policy_lock_authority_invalid")
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + 5
        while not acquired:
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    raise ValueError("policy_lock_platform_unsupported")
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("policy_owner_busy") from None
                time.sleep(.05)
        yield
    finally:
        if acquired:
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _install_policy_atomically(destination: Path, policy: Mapping[str, Any], *, expected_digest: str | None = None) -> str:
    from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
    error = _v2_no_symlink(destination, missing_ok=True)
    if error:
        return error
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _policy_lock(destination.with_name("." + destination.name + ".publish.lock")):
            return _install_locked_policy(destination, policy, expected_digest=expected_digest)
    except ValueError as exc:
        return str(exc)


def _install_locked_policy(destination: Path, policy: Mapping[str, Any], *, expected_digest: str | None) -> str:
    """Publish complete private bytes without following or truncating a link."""
    from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
    error = _v2_no_symlink(destination, missing_ok=True)
    if error:
        return error
    if expected_digest is not None:
        from eimemory.governance.evolution.code_automation_policy import _secure_read_v2_policy, _policy_digest
        if destination.exists():
            raw, error = _secure_read_v2_policy(destination)
            if error:
                return error
            try:
                actual = _policy_digest(json.loads(raw))
            except (TypeError, ValueError):
                return "existing_policy_invalid"
            if actual != expected_digest:
                return "existing_policy_changed"
        elif expected_digest:
            return "existing_policy_changed"
    destination.parent.mkdir(parents=True, exist_ok=True)
    error = _v2_no_symlink(destination, missing_ok=True)
    if error:
        return error
    descriptor, name = tempfile.mkstemp(prefix=".code-automation-policy-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(policy, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        error = _v2_no_symlink(destination, missing_ok=True)
        if error:
            return error
        os.replace(temporary, destination)
        from eimemory.storage.atomic_file import _fsync_directory
        _fsync_directory(destination.parent)
        return ""
    finally:
        # Only the fresh same-directory temporary file belongs to this call.
        temporary.unlink(missing_ok=True)


def inspect_code_automation_policy(
    *, path: str | Path | None = None, incident_digest: str | None = None,
) -> dict[str, Any]:
    """Use the same secure native loader as effects; never open a runtime DB.

    This validates policy authority and an optional exact digest comparison.
    The router still verifies the original current incident and owner itself.
    """
    from eimemory.governance.evolution.code_automation_policy import load_code_automation_policy
    if incident_digest is not None and (
        not isinstance(incident_digest, str) or re.fullmatch(r"[0-9a-f]{64}", incident_digest) is None
    ):
        return {"ok": False, "status": "blocked", "reason": "incident_digest_invalid", "read_only": True}
    destination = path or os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or CODE_AUTOMATION_POLICY_DEFAULT_PATH
    report = load_code_automation_policy(path=destination)
    incident = report.get("incident") if isinstance(report.get("incident"), Mapping) else {}
    actual = incident.get("incident_digest")
    bound = isinstance(actual, str) and re.fullmatch(r"[0-9a-f]{64}", actual) is not None
    valid = report.get("ok") is True
    if valid and incident_digest is not None and actual != incident_digest:
        report = {**report, "ok": False, "status": "blocked", "reason": "incident_digest_mismatch"}
    return {**report, "read_only": True, "native_policy_valid": valid,
            "system_route_policy_bound": valid and bound,
            "current_incident_verified": False}


def maybe_auto_issue_next_policy(
    *,
    repo_root: str | Path | None = None,
    incident_class: str,
    detector_id: str,
    test_plan_id: str,
    effects_mode: EffectsMode = "all-disabled",
) -> dict[str, Any]:
    """Opt-in post-deploy hook. Writes only when AUTO_ISSUE_ENV is truthy."""

    if str(os.environ.get(AUTO_ISSUE_ENV) or "").strip() not in {"1", "true", "TRUE", "yes", "YES"}:
        return {"ok": False, "reason": "auto_issue_disabled", "skipped": True}
    destination = Path(
        os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or CODE_AUTOMATION_POLICY_DEFAULT_PATH
    )
    return issue_code_automation_policy(
        repo_root=repo_root,
        incident_class=incident_class,
        detector_id=detector_id,
        test_plan_id=test_plan_id,
        effects_mode=effects_mode,
        install_path=destination,
    )


def _default_repo_root() -> Path:
    try:
        from eimemory.config.trusted import trusted_repository_root

        return trusted_repository_root()
    except Exception:
        return Path.cwd()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


__all__ = [
    "AUTO_ISSUE_ENV",
    "EffectsMode",
    "issue_code_automation_policy",
    "inspect_code_automation_policy",
    "maybe_auto_issue_next_policy",
]
