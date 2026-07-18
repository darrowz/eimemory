from __future__ import annotations

import json
import os
import sys
from typing import Any

from eimemory.llm.command_client import run_bounded_command


MAX_OPENCLAW_PROMPT_BYTES = 128 * 1024


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
    binary = str(os.environ.get("EIMEMORY_OPENCLAW_BIN") or "openclaw").strip()
    model = str(os.environ.get("EIMEMORY_LLM_MODEL") or "").strip()
    json_mode = payload.get("json_mode") is True
    try:
        configured_timeout = int(os.environ.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or 90)
    except ValueError:
        configured_timeout = 90
    timeout = max(1, min(600, configured_timeout if configured_timeout > 0 else 90))
    format_policy = (
        "JSON_MODE=true. Return only one strict JSON object or array with no markdown fences.\n\n"
        if json_mode
        else "JSON_MODE=false.\n\n"
    )
    combined = (
        "Follow the SYSTEM_POLICY below, then answer USER_REQUEST. Return strict JSON only when the request asks for it.\n\n"
        f"{format_policy}"
        f"<SYSTEM_POLICY>\n{system_prompt}\n</SYSTEM_POLICY>\n\n"
        f"<USER_REQUEST>\n{user_prompt}\n</USER_REQUEST>"
    )
    if len(combined.encode("utf-8")) > MAX_OPENCLAW_PROMPT_BYTES:
        raise ValueError("OpenClaw LLM prompt exceeds size limit")
    argv = [binary, "infer", "model", "run", "--prompt", combined, "--json"]
    if model:
        argv[4:4] = ["--model", model]
    completed = run_bounded_command(argv, b"", timeout_seconds=timeout)
    if completed[0] != 0:
        raise RuntimeError(f"OpenClaw inference failed with exit code {completed[0]}")
    response = json.loads(completed[1].decode("utf-8"))
    if not isinstance(response, dict):
        raise ValueError("OpenClaw inference response must be an object")
    outputs = response.get("outputs") if isinstance(response, dict) and isinstance(response.get("outputs"), list) else []
    text = "\n".join(
        str(item.get("text") or "").strip() for item in outputs if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    provider = str(response.get("provider") or "").strip()
    resolved_model = str(response.get("model") or model or "").strip()
    if response.get("ok") is not True or not text or not provider or not resolved_model:
        raise ValueError("OpenClaw inference response is incomplete")
    return {"text": text, "provider_id": provider, "model_id": f"{provider}/{resolved_model}"}


if __name__ == "__main__":
    raise SystemExit(main())
