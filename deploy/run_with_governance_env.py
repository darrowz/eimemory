#!/usr/bin/env python3
"""Execute a release gate with a narrowly allowlisted governance environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import stat
from typing import Mapping


MAX_ENV_BYTES = 64 * 1024
ALLOWED_KEYS = frozenset(
    {
        "EIMEMORY_LLM_COMMAND",
        "EIMEMORY_LLM_MODEL",
        "EIMEMORY_LLM_TIMEOUT_SECONDS",
        "EIMEMORY_OPENCLAW_BIN",
        "EIMEMORY_PROMPT_SAFETY_API_KEY",
        "EIMEMORY_PROMPT_SAFETY_BASE_URL",
        "EIMEMORY_PROMPT_SAFETY_COMMAND",
        "EIMEMORY_PROMPT_SAFETY_MAX_ATTEMPTS",
        "EIMEMORY_PROMPT_SAFETY_MODEL",
        "EIMEMORY_PROMPT_SAFETY_PROMPT",
        "EIMEMORY_PROMPT_SAFETY_PROMPT_FILES",
        "EIMEMORY_PROMPT_SAFETY_TIMEOUT_SECONDS",
    }
)
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class GovernanceEnvironmentError(RuntimeError):
    """Raised when the governance environment file is unsafe or ambiguous."""


def load_governance_environment(
    path: str | Path,
    *,
    base_environment: Mapping[str, str] | None = None,
    optional: bool = False,
) -> dict[str, str]:
    target = Path(path)
    environment = dict(os.environ if base_environment is None else base_environment)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        if optional:
            return environment
        raise GovernanceEnvironmentError("governance environment file is missing") from None
    except OSError as exc:
        raise GovernanceEnvironmentError("governance environment file cannot be opened safely") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise GovernanceEnvironmentError("governance environment file must be a regular non-symlink file")
            if metadata.st_size > MAX_ENV_BYTES:
                raise GovernanceEnvironmentError("governance environment file exceeds size limit")
            if os.name == "posix":
                allowed_owners = {0, os.geteuid()}
                if metadata.st_uid not in allowed_owners or metadata.st_mode & 0o022:
                    raise GovernanceEnvironmentError("governance environment file ownership or mode is unsafe")
            raw = handle.read(MAX_ENV_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GovernanceEnvironmentError("governance environment file cannot be read as UTF-8") from exc
    if len(raw) > MAX_ENV_BYTES or "\x00" in text:
        raise GovernanceEnvironmentError("governance environment file contains invalid data")

    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise GovernanceEnvironmentError(f"governance environment line {line_number} is not an assignment")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if KEY_PATTERN.fullmatch(key) is None:
            raise GovernanceEnvironmentError(f"governance environment line {line_number} has an invalid key")
        if key in seen:
            raise GovernanceEnvironmentError(f"governance environment key is duplicated: {key}")
        seen.add(key)
        value = _parse_value(raw_value.strip(), line_number=line_number)
        if key in ALLOWED_KEYS:
            environment[key] = value
    return environment


def _parse_value(raw_value: str, *, line_number: int) -> str:
    if not raw_value:
        return ""
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        parts = list(lexer)
    except ValueError as exc:
        raise GovernanceEnvironmentError(f"governance environment line {line_number} has invalid quoting") from exc
    if len(parts) != 1:
        raise GovernanceEnvironmentError(f"governance environment line {line_number} must contain one quoted value")
    return parts[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--optional", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    try:
        environment = load_governance_environment(
            args.env_file,
            optional=bool(args.optional),
        )
        os.execvpe(command[0], command, environment)
    except GovernanceEnvironmentError as exc:
        parser.exit(2, f"governance environment rejected: {exc}\n")
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
