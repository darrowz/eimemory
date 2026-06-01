from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record an outcome trace through the eimemory RPC bridge.")
    parser.add_argument("json_path")
    parser.add_argument("--url", default="http://127.0.0.1:8091/")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--agent-id", default="hongtu")
    parser.add_argument("--workspace-id", default="embodied")
    parser.add_argument("--user-id", default="darrow")
    return parser


def main(argv: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(argv)
    try:
        payload = json.loads(Path(parsed.json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": "invalid_json", "detail": str(exc)}, ensure_ascii=False))
        return 2
    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "error": "invalid_payload"}, ensure_ascii=False))
        return 2

    scope = {
        key: value
        for key, value in {
            "tenant_id": parsed.tenant_id,
            "agent_id": parsed.agent_id,
            "workspace_id": parsed.workspace_id,
            "user_id": parsed.user_id,
        }.items()
        if value
    }
    request = {
        "method": "experience.record_outcome_trace",
        "params": {
            "scope": scope,
            "payload": payload,
        },
    }
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        parsed.url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        result = json.loads(exc.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": "rpc_request_failed", "detail": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
