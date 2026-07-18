from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


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
    timeout = max(1, min(600, int(os.environ.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or 90)))
    combined = (
        "Follow the SYSTEM_POLICY below, then answer USER_REQUEST. Return strict JSON only when the request asks for it.\n\n"
        f"<SYSTEM_POLICY>\n{system_prompt}\n</SYSTEM_POLICY>\n\n"
        f"<USER_REQUEST>\n{user_prompt}\n</USER_REQUEST>"
    )
    argv = [binary, "infer", "model", "run", "--prompt", combined, "--json"]
    if model:
        argv[4:4] = ["--model", model]
    completed = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"OpenClaw inference failed with exit code {completed.returncode}")
    response = json.loads(completed.stdout)
    outputs = response.get("outputs") if isinstance(response, dict) and isinstance(response.get("outputs"), list) else []
    text = "\n".join(
        str(item.get("text") or "").strip() for item in outputs if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    provider = str(response.get("provider") or "").strip() if isinstance(response, dict) else ""
    resolved_model = str(response.get("model") or model or "").strip() if isinstance(response, dict) else model
    if response.get("ok") is not True or not text or not provider or not resolved_model:
        raise ValueError("OpenClaw inference response is incomplete")
    return {"text": text, "provider_id": provider, "model_id": f"{provider}/{resolved_model}"}


if __name__ == "__main__":
    raise SystemExit(main())
