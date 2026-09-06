from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any

from eimemory.llm.command_client import CommandLLMClient, run_bounded_command


MAX_HERMES_PROMPT_BYTES = 128 * 1024
_HERMES_MODEL_FLAGS = frozenset({"-m", "--model", "--provider"})


def hermes_llm_argv() -> list[str]:
    """Build a Hermes completion command that follows the live Hermes config.

    Do not pin a provider or model name. Hermes config.yaml / routing decides.
    """

    binary = str(os.environ.get("EIMEMORY_HERMES_BIN") or shutil.which("hermes") or "hermes").strip()
    return [binary, "--cli", "--safe-mode"]


def _hermes_binary() -> str:
    return str(os.environ.get("EIMEMORY_HERMES_BIN") or shutil.which("hermes") or "").strip()


def hermes_llm_client(*, timeout_seconds: int = 90) -> CommandLLMClient | None:
    binary = _hermes_binary()
    if not binary:
        return None
    if os.path.sep in binary and not os.path.exists(binary):
        return None
    if os.path.sep not in binary and shutil.which(binary) is None:
        return None
    return CommandLLMClient(
        [sys.executable, "-m", "eimemory.llm.hermes_adapter"],
        timeout_seconds=timeout_seconds,
    )


def resolve_l1_llm_client() -> CommandLLMClient | None:
    """Prefer an explicit command env, otherwise Hermes' currently configured model."""

    from eimemory.llm.command_client import llm_client_from_env

    explicit = llm_client_from_env("L1_EXTRACT")
    if explicit is not None:
        return explicit
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return hermes_llm_client()


def main() -> int:
    try:
        request = json.load(sys.stdin)
        result = complete_request(request)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def complete_request(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    system_prompt = str(payload.get("system_prompt") or "").strip()
    user_prompt = str(payload.get("user_prompt") or "").strip()
    if not user_prompt:
        raise ValueError("user_prompt is required")
    json_mode = payload.get("json_mode") is True
    try:
        configured_timeout = int(os.environ.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or 90)
    except ValueError:
        configured_timeout = 90
    timeout = max(1, min(600, configured_timeout if configured_timeout > 0 else 90))
    format_policy = (
        "Return only one JSON array. No markdown fences.\n\n" if json_mode else ""
    )
    combined = (
        f"{format_policy}"
        f"<SYSTEM_POLICY>\n{system_prompt}\n</SYSTEM_POLICY>\n\n"
        f"<USER_REQUEST>\n{user_prompt}\n</USER_REQUEST>"
    )
    if len(combined.encode("utf-8")) > MAX_HERMES_PROMPT_BYTES:
        raise ValueError("Hermes LLM prompt exceeds size limit")
    argv = [*hermes_llm_argv(), "-z", combined]
    if any(flag in argv for flag in _HERMES_MODEL_FLAGS):
        raise RuntimeError("Hermes L1 extract must not pin --model or --provider")
    completed = run_bounded_command(argv, b"", timeout_seconds=timeout)
    if completed[0] != 0:
        stderr = completed[2].decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"Hermes inference failed with exit code {completed[0]}: {stderr}")
    text = completed[1].decode("utf-8").strip()
    if not text:
        raise ValueError("Hermes inference returned empty output")
    if json_mode:
        text = _extract_json_payload(text)
    return {
        "text": text,
        "provider_id": "hermes",
        "model_id": "hermes/configured",
    }


def _extract_json_payload(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


if __name__ == "__main__":
    raise SystemExit(main())
