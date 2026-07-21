from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient


MAX_HOOK_CONTEXT_CHARS = 7_200
MAX_PROMPT_CHARS = 8_000
MAX_TOOL_INPUT_PREVIEW_CHARS = 2_000
MAX_TOOL_RESULT_PREVIEW_CHARS = 4_000
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r'''(?i)(["']?[a-z0-9_-]*(?:api[_-]?key|token|secret|password|private[_-]?key|authorization|cookie)(?:[_-]?(?:s|v\d+))?["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}\]]+)'''
    ),
)
_KEY_CANONICAL = re.compile(r"[^a-z0-9]")
_SENSITIVE_KEY_SUFFIX = re.compile(
    r"(?:apikey|token|secret|password|privatekey|authorization|cookie)(?:s|v\d+)?$"
)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "secret",
        "password",
        "authorization",
        "cookie",
        "cookies",
        "privatekey",
        "credential",
        "credentials",
    }
)


def codex_scope_from_env(*, cwd: str = "") -> dict[str, str]:
    workspace = os.getenv("EIMEMORY_WORKSPACE_ID", "").strip()
    if not workspace:
        workspace = Path(str(cwd or ".")).name.strip() or "default"
    return {
        "tenant_id": os.getenv("EIMEMORY_TENANT_ID", "default").strip() or "default",
        "agent_id": os.getenv("EIMEMORY_AGENT_ID", "codex").strip() or "codex",
        "workspace_id": workspace,
        "user_id": os.getenv("EIMEMORY_USER_ID", "default").strip() or "default",
    }


def codex_client_from_env() -> AgentRuntimeRPCClient:
    timeout_text = os.getenv("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", "0.8")
    try:
        timeout_seconds = float(timeout_text)
    except ValueError:
        timeout_seconds = 0.8
    ledger = os.getenv("EIMEMORY_ADAPTER_FAILURE_LEDGER", "").strip()
    if not ledger:
        plugin_data = os.getenv("PLUGIN_DATA", "").strip()
        ledger = str(Path(plugin_data) / "eimemory-failures.jsonl") if plugin_data else ""
    return AgentRuntimeRPCClient(
        base_url=os.getenv("EIMEMORY_RPC_URL", "http://127.0.0.1:8091/").strip(),
        auth_token=os.getenv("EIMEMORY_RPC_TOKEN", "").strip(),
        timeout_seconds=timeout_seconds,
        failure_ledger_path=ledger or None,
    )


class CodexHookAdapter:
    def __init__(self, *, client: Any, scope: Mapping[str, str] | None = None) -> None:
        self.client = client
        self.scope = dict(scope) if scope is not None else None

    def handle(self, event_name: str, event: Mapping[str, Any] | None) -> dict[str, Any]:
        name = str(event_name or "").strip()
        payload = dict(event or {})
        try:
            if name == "SessionStart":
                return self._prefetch(name, payload, self._session_query(payload), "code.session")
            if name == "UserPromptSubmit":
                query = _bounded_text(payload.get("prompt") or payload.get("user_prompt"), MAX_PROMPT_CHARS)
                return self._prefetch(name, payload, query, _task_type(payload, default="code.task"))
            if name == "PostToolUse":
                self._sync_tool_use(payload)
                return {"continue": True}
            if name == "Stop":
                self._record_stop(payload)
                return {"continue": True}
        except Exception as exc:  # noqa: BLE001 - hook transport is intentionally fail-open
            safe_event_name = re.sub(r"[\r\n]+", " ", name)[:80] or "unknown"
            sys.stderr.write(
                f"eimemory codex-hook: {safe_event_name} dispatch failed ({type(exc).__name__})\n"
            )
            return {"continue": True}
        return {"continue": True}

    def _prefetch(
        self,
        event_name: str,
        event: Mapping[str, Any],
        query: str,
        task_type: str,
    ) -> dict[str, Any]:
        result = self.client.call_or_bypass(
            "adapter.prefetch",
            {
                "channel": "codex",
                "scope": self._scope_for_event(event),
                "query": query,
                "task_type": task_type,
                "limit": 8,
                "task_context": {
                    "cwd": _bounded_text(event.get("cwd"), 1_000),
                    "model": _bounded_text(event.get("model"), 200),
                },
            },
        )
        if result.get("ok") is not True or not isinstance(result.get("result"), dict):
            return {"continue": True}
        context = _bounded_text(result["result"].get("context"), MAX_HOOK_CONTEXT_CHARS)
        if not context:
            return {"continue": True}
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            },
        }

    def _sync_tool_use(self, event: Mapping[str, Any]) -> None:
        session_id = _event_id(event, "session_id", default="")
        turn_id = _event_id(event, "turn_id", "tool_call_id", "tool_use_id", default="")
        if not session_id or not turn_id:
            return
        tool_name = _bounded_text(event.get("tool_name") or event.get("tool"), 200) or "unknown"
        tool_input = event.get("tool_input", event.get("input", {}))
        tool_result = event.get("tool_response", event.get("tool_result", event.get("tool_output", "")))
        input_summary = _safe_summary(tool_input, preview_limit=MAX_TOOL_INPUT_PREVIEW_CHARS)
        result_summary = _safe_summary(tool_result, preview_limit=MAX_TOOL_RESULT_PREVIEW_CHARS)
        self.client.call_or_bypass(
            "adapter.sync_turn",
            {
                "channel": "codex",
                "scope": self._scope_for_event(event),
                "session_id": session_id,
                "turn_id": f"{turn_id}:tool",
                "user_text": f"Tool {tool_name}; input_{input_summary}",
                "assistant_text": f"Tool {tool_name}; result_{result_summary}",
            },
        )

    def _record_stop(self, event: Mapping[str, Any]) -> None:
        session_id = _event_id(event, "session_id", default="")
        event_id = _event_id(event, "turn_id", "event_id", default="")
        if not session_id or not event_id:
            return
        success_value = event.get("success")
        success = success_value if isinstance(success_value, bool) else None
        verification = _bounded_text(
            event.get("verification")
            or event.get("verification_evidence")
            or event.get("eimemory_verification"),
            512,
        )
        result = _bounded_text(
            event.get("last_assistant_message") or event.get("result") or event.get("response"),
            2_000,
        )
        receipts = event.get("verification_receipts")
        self.client.call_or_bypass(
            "adapter.record_terminal",
            {
                "channel": "codex",
                "scope": self._scope_for_event(event),
                "end_kind": "stop",
                "session_id": session_id,
                "event_id": event_id,
                "task_type": _task_type(event, default="code.task"),
                "success": success,
                "verification": verification,
                "result": result,
                "tool_receipts": list(receipts)[:32] if isinstance(receipts, list) else [],
                "rehearsal": False,
            },
        )

    def _scope_for_event(self, event: Mapping[str, Any]) -> dict[str, str]:
        if self.scope:
            return dict(self.scope)
        return codex_scope_from_env(cwd=str(event.get("cwd") or ""))

    @staticmethod
    def _session_query(event: Mapping[str, Any]) -> str:
        cwd = _bounded_text(event.get("cwd"), 1_000)
        model = _bounded_text(event.get("model"), 200)
        return _bounded_text(f"Resume Codex work in {cwd} using {model}".strip(), MAX_PROMPT_CHARS)


def _task_type(event: Mapping[str, Any], *, default: str) -> str:
    return _bounded_text(event.get("task_type") or event.get("eimemory_task_type"), 200) or default


def _event_id(event: Mapping[str, Any], *names: str, default: str) -> str:
    for name in names:
        value = _bounded_text(event.get(name), 500)
        if value:
            return value
    return default


def _safe_summary(value: Any, *, preview_limit: int) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        raw = repr(value)
    if isinstance(value, str):
        redacted = _redact_text(value)
    else:
        try:
            redacted = json.dumps(_redact_structured(value), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError, RecursionError):
            redacted = _redact_text(raw)
    digest = sha256(redacted.encode("utf-8", errors="replace")).hexdigest()
    preview = _bounded_text(redacted, preview_limit)
    return f"sha256={digest}; preview={preview}"


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_structured(value: Any, *, depth: int = 0) -> Any:
    if depth >= 16:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            canonical = _KEY_CANONICAL.sub("", key_text.lower())
            sensitive = canonical in _SENSITIVE_KEY_NAMES or _SENSITIVE_KEY_SUFFIX.search(canonical) is not None
            redacted[key_text] = (
                "[REDACTED]"
                if sensitive
                else _redact_structured(item, depth=depth + 1)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_structured(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit]


def run_hook_from_stdio(event_name: str, *, stdin: Any = None, stdout: Any = None) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    try:
        event = json.loads(input_stream.read() or "{}")
        if not isinstance(event, dict):
            event = {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        event = {}
    adapter = CodexHookAdapter(
        client=codex_client_from_env(),
        scope=codex_scope_from_env(cwd=str(event.get("cwd") or "")),
    )
    result = adapter.handle(event_name, event)
    output_stream.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eimemory codex-hook")
    parser.add_argument("--event", required=True)
    parsed = parser.parse_args(argv)
    return run_hook_from_stdio(parsed.event)


if __name__ == "__main__":
    raise SystemExit(main())
