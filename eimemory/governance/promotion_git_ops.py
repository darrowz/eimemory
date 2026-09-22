"""Git / patch-exec helpers extracted from promotion_manager (god-file slice)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "apply", "enabled"}

def _v1_verification_environment(*, cache_root: str) -> dict[str, str]:
    """Minimal env for v1 verify subprocess (CE-1).

    Do not inherit the parent process environment: tokens, receipt keys, and
    other secrets must not leak into sandbox verification. Mirrors the v2
    ``_verification_environment`` allowlist shape.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(cache_root),
        "TMPDIR": "/tmp/temp",
        "TMP": "/tmp/temp",
        "TEMP": "/tmp/temp",
        "XDG_CACHE_HOME": "/tmp/cache",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }

def _run_patch_subprocess(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    """Run a patch command while keeping verification caches out of the repo."""
    if not str(phase or "").startswith("verify"):
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    with tempfile.TemporaryDirectory(prefix="eimemory-code-verify-") as cache_root:
        environment = _v1_verification_environment(cache_root=cache_root)
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
            env=environment,
        )

def _resolve_patch_command(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = str(command[0] or "")
    lower = executable.lower()
    if lower in {"python", "python.exe", "python3", "python3.exe"}:
        return [sys.executable, *[str(part) for part in command[1:]]]
    return [str(part) for part in command]

def _normalize_commands(commands: Any) -> list[str | list[str]]:
    if commands is None:
        return []
    if isinstance(commands, str):
        return [commands] if commands.strip() else []
    if not isinstance(commands, list):
        return []
    normalized: list[str | list[str]] = []
    for item in commands:
        if isinstance(item, str):
            if item.strip():
                normalized.append(item)
        elif isinstance(item, (list, tuple)) and item:
            normalized.append([str(part) for part in item])
    return normalized

def _normalize_env_commands(name: str) -> list[list[str]]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if _is_argv_command(parsed):
        return [_coerce_argv_command(parsed)]
    if not isinstance(parsed, list):
        return []
    return [_coerce_argv_command(item) for item in parsed if _is_argv_command(item)]

def _is_argv_command(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        not isinstance(part, (dict, list, tuple)) for part in value
    )

def _coerce_argv_command(value: Any) -> list[str]:
    return [str(part) for part in value]

def _run_patch_commands(commands: Any, *, cwd: Path, timeout_seconds: int, phase: str) -> dict[str, Any]:
    normalized = _normalize_commands(commands)
    if not normalized and str(phase or "").startswith("verify"):
        return {
            "ok": False,
            "reports": [],
            "skipped": True,
            "error_type": "missing_required_commands",
        }
    reports: list[dict[str, Any]] = []
    for command in normalized:
        if isinstance(command, str):
            report = {
                "phase": phase,
                "command": command,
                "returncode": None,
                "stdout": "",
                "stderr": "shell string commands are not supported; provide argv JSON/list commands",
                "ok": False,
                "error_type": "unsupported_shell_command",
            }
            reports.append(report)
            return {"ok": False, "reports": reports}
        run_command = _resolve_patch_command(command)
        display = [str(part) for part in run_command]
        try:
            completed = _run_patch_subprocess(
                run_command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                phase=phase,
            )
            report = {
                "phase": phase,
                "command": display,
                "returncode": completed.returncode,
                "stdout": (completed.stdout or "")[-4000:],
                "stderr": (completed.stderr or "")[-4000:],
                "ok": completed.returncode == 0,
            }
        except subprocess.TimeoutExpired as exc:
            report = {
                "phase": phase,
                "command": display,
                "returncode": None,
                "stdout": str(exc.stdout or "")[-4000:],
                "stderr": str(exc.stderr or "")[-4000:],
                "ok": False,
                "timeout": True,
            }
        except Exception as exc:
            report = {
                "phase": phase,
                "command": display,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "ok": False,
                "error_type": type(exc).__name__,
            }
        reports.append(report)
        if not report["ok"]:
            return {"ok": False, "reports": reports}
    return {"ok": True, "reports": reports, "skipped": not bool(normalized)}

def _repo_has_dirty_worktree(repo_root: Path) -> bool:
    if not (repo_root / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return True
    return result.returncode != 0 or bool(result.stdout.strip())

def _reset_repo_to_commit(repo_root: Path, *, prior_commit_sha: str, timeout_seconds: int) -> dict[str, Any]:
    sha = str(prior_commit_sha or "").strip()
    if not sha:
        return {"ok": True, "skipped": True, "reason": "prior_commit_missing"}
    if not (repo_root / ".git").exists():
        return {"ok": True, "skipped": True, "reason": "git_repo_missing"}
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if verify.returncode != 0:
        return {
            "ok": False,
            "skipped": False,
            "reason": "prior_commit_not_found",
            "stderr": (verify.stderr or "")[-4000:],
        }
    reset = _run_patch_commands(
        [["git", "reset", "--hard", sha]],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
        phase="rollback:git_reset",
    )
    clean = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "ok": bool(reset.get("ok")) and clean.returncode == 0 and not clean.stdout.strip(),
        "skipped": False,
        "prior_commit_sha": sha,
        "reports": list(reset.get("reports") or []),
        "dirty_after_reset": clean.stdout.strip(),
        "status_stderr": (clean.stderr or "")[-4000:],
    }

def _commit_repo_patch(
    repo_root: Path,
    *,
    applied_paths: list[str],
    patch: dict[str, Any],
    candidate: RecordEnvelope,
    timeout_seconds: int,
    automation_policy: dict[str, Any],
) -> dict[str, Any]:
    if not _truthy(patch.get("commit_to_repo"), default=False):
        return {"ok": True, "skipped": True, "reason": "commit_disabled"}
    if not bool(automation_policy.get("allow_commit")):
        return {"ok": False, "reason": "automation_policy_commit_not_enabled"}
    if not (repo_root / ".git").exists():
        return {"ok": False, "reason": "git_repo_missing"}
    add_result = _run_patch_commands([["git", "add", "--", *applied_paths]], cwd=repo_root, timeout_seconds=timeout_seconds, phase="commit")
    if not add_result["ok"]:
        return {"ok": False, "reason": "git_add_failed", "reports": add_result["reports"]}
    diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    if diff_result.returncode == 0:
        return {"ok": True, "skipped": True, "reason": "no_staged_changes", "reports": add_result["reports"]}
    message = str(patch.get("commit_message") or f"autonomous: apply code patch {candidate.record_id[:12]}")
    commit_result = _run_patch_commands([["git", "commit", "-m", message]], cwd=repo_root, timeout_seconds=timeout_seconds, phase="commit")
    if not commit_result["ok"]:
        return {"ok": False, "reason": "git_commit_failed", "reports": add_result["reports"] + commit_result["reports"]}
    sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    return {
        "ok": sha_result.returncode == 0,
        "commit_sha": sha_result.stdout.strip() if sha_result.returncode == 0 else "",
        "reports": add_result["reports"] + commit_result["reports"],
    }

def _current_commit_sha(repo_root: Path, *, timeout_seconds: int) -> str:
    if not (repo_root / ".git").exists():
        return ""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
