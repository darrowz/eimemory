from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    provider_id: str
    model_id: str


class CommandLLMClient:
    """Provider-neutral JSON-stdin/JSON-stdout LLM command client."""

    def __init__(self, argv: list[str] | tuple[str, ...], *, timeout_seconds: int = 90) -> None:
        normalized = tuple(str(item).strip() for item in argv if str(item).strip())
        if not normalized:
            raise ValueError("LLM command argv is empty")
        self.argv = normalized
        self.timeout_seconds = max(1, min(600, int(timeout_seconds)))

    def complete(self, *, system_prompt: str, user_prompt: str, json_mode: bool = False) -> LLMResult:
        request = json.dumps(
            {
                "system_prompt": str(system_prompt),
                "user_prompt": str(user_prompt),
                "json_mode": bool(json_mode),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        completed = subprocess.run(
            list(self.argv),
            input=request,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"LLM command failed with exit code {completed.returncode}")
        if not completed.stdout or len(completed.stdout.encode("utf-8")) > 2_000_000:
            raise ValueError("LLM command returned an empty or oversized response")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("LLM command response must be an object")
        text = str(payload.get("text") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip()
        if not text or not provider_id or not model_id:
            raise ValueError("LLM command response requires text, provider_id, and model_id")
        return LLMResult(text=text, provider_id=provider_id, model_id=model_id)


def llm_client_from_env(feature: str = "") -> CommandLLMClient | None:
    prefix = str(feature or "").strip().upper().replace("-", "_")
    specific = f"EIMEMORY_{prefix}_LLM_COMMAND" if prefix else ""
    raw = str((os.environ.get(specific) if specific else "") or os.environ.get("EIMEMORY_LLM_COMMAND") or "").strip()
    if not raw:
        return None
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{specific or 'EIMEMORY_LLM_COMMAND'} must be a JSON argv array") from exc
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
        raise ValueError(f"{specific or 'EIMEMORY_LLM_COMMAND'} must be a non-empty JSON argv array")
    timeout_name = f"EIMEMORY_{prefix}_LLM_TIMEOUT_SECONDS" if prefix else ""
    timeout_raw = (os.environ.get(timeout_name) if timeout_name else "") or os.environ.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or "90"
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 90
    return CommandLLMClient(argv, timeout_seconds=timeout)
