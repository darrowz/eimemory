from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eimemory.llm.command_client import run_bounded_command


MAX_RESULT_BYTES = 1_000_000
MAX_PROMPT_BYTES = 1_000_000


class CommandPromptSafetyExecutor:
    """Execute one prompt-safety case through an operator-configured argv."""

    def __init__(self, argv: list[str] | tuple[str, ...], *, timeout_seconds: int = 90) -> None:
        normalized = tuple(str(item) for item in argv)
        if not normalized or any(not item.strip() for item in normalized):
            raise ValueError("prompt safety command argv is empty")
        self.argv = normalized
        self.timeout_seconds = max(1, min(600, int(timeout_seconds)))

    def execute_case(self, *, system_prompt: str, case: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
        request = json.dumps(
            {"system_prompt": str(system_prompt), "case": dict(case), "release": dict(release)},
            ensure_ascii=False,
            sort_keys=True,
        )
        completed = run_bounded_command(
            list(self.argv),
            request.encode("utf-8"),
            timeout_seconds=self.timeout_seconds,
        )
        if completed[0] != 0:
            raise RuntimeError(f"prompt safety command failed with exit code {completed[0]}")
        raw = completed[1]
        if not raw or len(raw) > MAX_RESULT_BYTES:
            raise ValueError("prompt safety command returned an empty or oversized result")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("prompt safety command result must be an object")
        return payload


def prompt_safety_executor_from_env() -> CommandPromptSafetyExecutor | None:
    raw = str(os.environ.get("EIMEMORY_PROMPT_SAFETY_COMMAND") or "").strip()
    if not raw:
        return None
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("EIMEMORY_PROMPT_SAFETY_COMMAND must be a JSON argv array") from exc
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
        raise ValueError("EIMEMORY_PROMPT_SAFETY_COMMAND must be a non-empty JSON argv array")
    timeout = _positive_int(os.environ.get("EIMEMORY_PROMPT_SAFETY_TIMEOUT_SECONDS"), default=90)
    return CommandPromptSafetyExecutor(argv, timeout_seconds=timeout)


def prompt_safety_prompt_from_env() -> str:
    inline = str(os.environ.get("EIMEMORY_PROMPT_SAFETY_PROMPT") or "").strip()
    raw_files = str(os.environ.get("EIMEMORY_PROMPT_SAFETY_PROMPT_FILES") or "").strip()
    paths: list[str] = []
    if raw_files:
        try:
            parsed = json.loads(raw_files)
        except json.JSONDecodeError as exc:
            raise ValueError("EIMEMORY_PROMPT_SAFETY_PROMPT_FILES must be a JSON path array") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) and item.strip() for item in parsed):
            raise ValueError("EIMEMORY_PROMPT_SAFETY_PROMPT_FILES must be a JSON path array")
        paths = [str(item).strip() for item in parsed]
    sections = [inline] if inline else []
    total_bytes = len(inline.encode("utf-8"))
    if total_bytes > MAX_PROMPT_BYTES:
        raise ValueError("configured prompt safety prompt exceeds size limit")
    for item in paths:
        path = Path(item).expanduser()
        remaining = MAX_PROMPT_BYTES - total_bytes
        with path.open("rb") as handle:
            data = handle.read(remaining + 1)
        total_bytes += len(data)
        if total_bytes > MAX_PROMPT_BYTES:
            raise ValueError("configured prompt safety prompt exceeds size limit")
        sections.append(f"# {path.name}\n{data.decode('utf-8')}")
    return "\n\n".join(section for section in sections if section.strip()).strip()


def _positive_int(value: Any, *, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default
