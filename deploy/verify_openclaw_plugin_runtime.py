#!/usr/bin/env python3
"""Verify that the loaded OpenClaw bridge matches its static contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PLUGIN_ID = "eimemory-bridge"
REQUIRED_HOOKS = {
    "after_tool_call",
    "agent_end",
    "before_agent_finalize",
    "before_prompt_build",
    "before_tool_call",
    "message_received",
    "message_sent",
    "session_end",
}
REQUIRED_TOOLS = {"eimemory_bridge_status", "memory_e2e_check"}
MAX_INSPECT_BYTES = 4 * 1024 * 1024


class OpenClawRuntimeError(RuntimeError):
    """Raised when runtime inspection contradicts the bridge contract."""


def _string_set(values: object, field: str) -> set[str]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise OpenClawRuntimeError(f"{field} must be a string array")
    return set(values)


def verify_openclaw_plugin_runtime(
    payload: object,
    *,
    expected_root: str | Path,
    allow_legacy_runtime: bool = False,
) -> dict[str, object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("plugin"), dict):
        raise OpenClawRuntimeError("runtime inspection payload is malformed")
    plugin = payload["plugin"]
    if plugin.get("id") != PLUGIN_ID:
        raise OpenClawRuntimeError("runtime plugin identity mismatch")
    if plugin.get("enabled") is not True or plugin.get("activated") is not True or plugin.get("status") != "loaded":
        raise OpenClawRuntimeError("runtime plugin is not enabled, activated, and loaded")

    root_value = plugin.get("rootDir")
    if not isinstance(root_value, str) or not root_value.strip():
        raise OpenClawRuntimeError("runtime plugin root is missing")
    try:
        actual_root = Path(root_value).resolve(strict=True)
        required_root = Path(expected_root).resolve(strict=True)
    except OSError as exc:
        raise OpenClawRuntimeError("runtime plugin root cannot be resolved") from exc
    if actual_root != required_root:
        raise OpenClawRuntimeError("runtime plugin root does not match the candidate release")

    tool_names = _string_set(plugin.get("toolNames"), "runtime tools")
    contracts = plugin.get("contracts")
    if not isinstance(contracts, dict):
        raise OpenClawRuntimeError("runtime contracts are missing")
    contract_tools = _string_set(contracts.get("tools"), "contract tools")
    if allow_legacy_runtime:
        if "eimemory_bridge_status" not in tool_names or not tool_names.issubset(contract_tools):
            raise OpenClawRuntimeError("legacy runtime tools contradict contracts.tools")
    elif tool_names != REQUIRED_TOOLS or contract_tools != REQUIRED_TOOLS or tool_names != contract_tools:
        raise OpenClawRuntimeError("runtime tools do not match contracts.tools")

    typed_hooks = payload.get("typedHooks")
    if not isinstance(typed_hooks, list) or any(not isinstance(item, dict) for item in typed_hooks):
        raise OpenClawRuntimeError("runtime typed hooks are malformed")
    hook_names = {str(item.get("name") or "") for item in typed_hooks}
    missing_hooks = REQUIRED_HOOKS - hook_names
    if missing_hooks and not allow_legacy_runtime:
        raise OpenClawRuntimeError(f"runtime typed hooks are missing: {','.join(sorted(missing_hooks))}")
    if payload.get("diagnostics") not in (None, []):
        raise OpenClawRuntimeError("runtime inspection contains diagnostics")
    if payload.get("compatibility") not in (None, []):
        raise OpenClawRuntimeError("runtime inspection reports compatibility adapters")
    return {
        "ok": True,
        "plugin_id": PLUGIN_ID,
        "hook_count": len(hook_names),
        "tool_count": len(tool_names),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-root", required=True, type=Path)
    parser.add_argument("--allow-legacy-runtime", action="store_true")
    args = parser.parse_args(argv)
    raw = sys.stdin.read(MAX_INSPECT_BYTES + 1)
    try:
        if len(raw.encode("utf-8")) > MAX_INSPECT_BYTES:
            raise OpenClawRuntimeError("runtime inspection payload is unexpectedly large")
        payload = json.loads(raw)
        report = verify_openclaw_plugin_runtime(
            payload,
            expected_root=args.expected_root,
            allow_legacy_runtime=args.allow_legacy_runtime,
        )
    except (UnicodeError, json.JSONDecodeError, OpenClawRuntimeError) as exc:
        parser.exit(2, f"OpenClaw runtime verification failed: {exc}\n")
    print(
        f"openclaw_plugin_runtime=ok hooks={report['hook_count']} tools={report['tool_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
