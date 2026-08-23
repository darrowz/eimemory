"""Fail-closed command policy for automatic local code-patch verification.

The proposer and replay inputs are data, not authority to run arbitrary local
or remote commands. Direct code application therefore accepts only a narrow
argv grammar that is sufficient for syntax compilation and focused pytest
checks. Deployment, commit, and rollback commands are separate explicitly
enabled operations and are not accepted through this verification contract.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any


AUTOMATION_POLICY_ACTIONS = ("local_apply", "commit", "deployment")


def protected_test_plan_command_error(
    commands: Any,
    *,
    plan_id: str,
    candidate_python: str | Path,
) -> str:
    """Validate serialized argv only against a trusted, immutable test plan.

    The legacy grammar below remains available to explicit compatibility
    callers.  New code-evolution transactions must use this plan-owned
    validator; provider or incident data cannot add targets, options, or
    interpreters.
    """

    from eimemory.governance.code_evolution_test_plans import (
        protected_test_plan_command_error as _protected_test_plan_command_error,
    )

    return _protected_test_plan_command_error(
        commands,
        plan_id=plan_id,
        candidate_python=candidate_python,
    )


def normalize_automation_policy(value: Any) -> dict[str, Any]:
    """Sanitize an untrusted policy-shaped value for diagnostics only.

    A proposal is data, never authority.  Promotion and dynamic evolution read
    their authority only from ``code_automation_policy.load_code_automation_policy``.
    This compatibility helper retains a bounded view for legacy callers but
    does not grant any machine action.  Unknown fields are discarded so
    diagnostics cannot echo policy secrets.

    The accepted input forms preserve the pre-existing ``allow_apply`` spelling
    while normalizing every caller to the distinct ``local_apply``, ``commit``,
    and ``deployment`` actions:

    ``{"policy_id": "...", "actions": {"local_apply": true}}``
    ``{"policy_id": "...", "allow_apply": true}``
    """
    raw = dict(value) if isinstance(value, dict) else {}
    actions = {
        action: _automation_policy_action_enabled(raw, action)
        for action in AUTOMATION_POLICY_ACTIONS
    }
    return {
        "declared": bool(raw),
        "policy_id": _bounded_policy_identifier(raw.get("policy_id")),
        "actions": actions,
    }


def _automation_policy_action_enabled(raw: dict[str, Any], action: str) -> bool:
    aliases = {
        "local_apply": ("local_apply", "allow_local_apply", "allow_apply"),
        "commit": ("commit", "allow_commit"),
        "deployment": ("deployment", "deploy", "allow_deployment", "allow_deploy"),
    }.get(action, (action,))
    declarations: list[bool] = []
    for container_name in ("actions", "capabilities"):
        container = raw.get(container_name)
        if isinstance(container, dict):
            for key in aliases:
                if key in container:
                    declarations.append(container.get(key) is True)
    for key in aliases:
        if key in raw:
            declarations.append(raw.get(key) is True)
    allowed_actions = raw.get("allowed_actions")
    if isinstance(allowed_actions, (list, tuple, set)):
        declarations.append(action in {str(item).strip() for item in allowed_actions})
    return bool(declarations) and all(declarations)


def _bounded_policy_identifier(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:160]


def code_patch_verification_command_error(commands: Any) -> str:
    """Return a stable rejection reason, or ``""`` for safe argv commands."""
    if not isinstance(commands, list) or not commands:
        return "code_patch_requires_verification_commands"
    for command in commands:
        if isinstance(command, str):
            return "verification_commands_must_be_argv"
        if not isinstance(command, (list, tuple)) or not command:
            return "verification_commands_must_be_argv"
        argv = [str(part) for part in command]
        if not _is_python_interpreter(argv[0]):
            return "code_patch_verification_command_not_allowed"
        if len(argv) >= 3 and argv[1:3] == ["-m", "compileall"]:
            command_error = _compileall_command_error(argv[3:])
        elif len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
            command_error = _pytest_command_error(argv[3:])
        else:
            command_error = "code_patch_verification_command_not_allowed"
        if command_error:
            return command_error
    return ""


def _is_python_interpreter(value: str) -> bool:
    raw = str(value or "").strip()
    if raw.lower() in {"python", "python.exe", "python3", "python3.exe"}:
        return True
    try:
        return Path(raw).resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _compileall_command_error(arguments: list[str]) -> str:
    targets: list[str] = []
    for argument in arguments:
        value = str(argument or "").strip()
        if value == "-q":
            continue
        if value.startswith("-"):
            return "code_patch_verification_compileall_option_not_allowed"
        if not _safe_relative_path(value):
            return "code_patch_verification_target_not_allowed"
        targets.append(value)
    return "" if targets else "code_patch_verification_requires_target"


def _pytest_command_error(arguments: list[str]) -> str:
    targets: list[str] = []
    for argument in arguments:
        value = str(argument or "").strip()
        if value in {"-q", "--quiet"}:
            continue
        if value.startswith("-"):
            return "code_patch_verification_pytest_option_not_allowed"
        path = value.split("::", 1)[0]
        if not _safe_relative_path(path) or not path.startswith("tests/") or not path.endswith(".py"):
            return "code_patch_verification_target_not_allowed"
        targets.append(value)
    return "" if targets else "code_patch_verification_requires_test_file"


def _safe_relative_path(value: str) -> bool:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return False
    path = PurePosixPath(raw)
    first = path.parts[0] if path.parts else ""
    if path.is_absolute() or raw.startswith("//") or ":" in first:
        return False
    return not any(part in {"", ".", ".."} for part in path.parts)
