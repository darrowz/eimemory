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
