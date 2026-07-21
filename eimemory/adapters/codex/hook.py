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
from eimemory.adapters.runtime.host_auth import producer_token_from_private_file
from eimemory.adapters.runtime.receipt_handoff import ReceiptIdHandoff


MAX_HOOK_CONTEXT_CHARS = 7_200
MAX_PROMPT_CHARS = 8_000
MAX_TOOL_INPUT_PREVIEW_CHARS = 2_000
MAX_TOOL_RESULT_PREVIEW_CHARS = 4_000
SUMMARY_REDACTION_SLACK_CHARS = 512
MAX_SUMMARY_REDACTION_NODES = 256
_TRUNCATED = "[TRUNCATED]"
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


def codex_attestation_client_from_env() -> AgentRuntimeRPCClient | None:
    token = producer_token_from_private_file("codex")
    if not token:
        return None
    client = codex_client_from_env()
    if token == client.auth_token:
        return None
    client.auth_token = token
    return client


class CodexHookAdapter:
    def __init__(self, *, client: Any, scope: Mapping[str, str] | None = None, attestation_client: Any | None = None) -> None:
        self.client = client
        self.scope = dict(scope) if scope is not None else None
        self.attestation_client = attestation_client
        self.receipt_handoff = ReceiptIdHandoff.from_env()

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
            sys.stderr.flush()
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
        scope = self._scope_for_event(event)
        if self.attestation_client is not None:
            result = self.attestation_client.call_or_bypass(
                "adapter.attest_tool_result",
                {
                    "channel": "codex", "scope": scope,
                    "session_id": session_id, "run_id": turn_id, "tool_call_id": _event_id(event, "tool_call_id", "tool_use_id", default=turn_id),
                    "tool_name": tool_name,
                    "tool_input": _redact_structured(tool_input),
                    "result": _redact_structured(tool_result),
                    "duration_ms": event.get("duration_ms", 0) if isinstance(event.get("duration_ms", 0), int) else 0,
                },
            )
            receipt_id = result.get("result", {}).get("receipt_id") if isinstance(result.get("result"), dict) else ""
            receipt = result.get("result", {}).get("receipt") if isinstance(result.get("result"), dict) else None
            if (
                isinstance(receipt_id, str)
                and receipt_id
                and isinstance(receipt, dict)
                and receipt.get("passed") is True
                and self.receipt_handoff is not None
            ):
                self.receipt_handoff.append(
                    channel="codex",
                    scope=scope,
                    session_id=session_id,
                    run_id=turn_id,
                    receipt_id=receipt_id,
                )
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
        result = _bounded_text(
            event.get("last_assistant_message") or event.get("result") or event.get("response"),
            2_000,
        )
        scope = self._scope_for_event(event)
        receipt_ids = (
            self.receipt_handoff.list_ids(
                channel="codex",
                scope=scope,
                session_id=session_id,
                run_id=event_id,
            )
            if self.receipt_handoff is not None
            else []
        )
        response = self.client.call_or_bypass(
            "adapter.record_terminal",
            {
                "channel": "codex",
                "scope": scope,
                "end_kind": "stop",
                "session_id": session_id,
                "event_id": event_id,
                # Codex Stop does not carry host-authenticated outcome fields.
                # The runtime derives these values exclusively from exact receipts.
                "task_type": "code.unverified",
                "success": None,
                "verification": "",
                "result": result,
                "tool_receipts": [],
                "receipt_ids": receipt_ids,
                "rehearsal": False,
            },
        )
        terminal_result = response.get("result") if isinstance(response, dict) else None
        if (
            response.get("ok") is True
            and isinstance(terminal_result, dict)
            and terminal_result.get("ok") is True
            and self.receipt_handoff is not None
        ):
            self.receipt_handoff.clear_exact(
                channel="codex",
                scope=scope,
                session_id=session_id,
                run_id=event_id,
                receipt_ids=receipt_ids,
            )

    def _scope_for_event(self, event: Mapping[str, Any]) -> dict[str, str]:
        if self.scope:
            return dict(self.scope)
        return codex_scope_from_env(cwd=str(event.get("cwd") or ""))

    @staticmethod
    def _session_query(event: Mapping[str, Any]) -> str:
        cwd = _bounded_text(event.get("cwd"), 1_000)
        model = _bounded_text(event.get("model"), 200)
        return _bounded_text(f"Resume Codex work in {cwd} using {model}", MAX_PROMPT_CHARS)


def _task_type(event: Mapping[str, Any], *, default: str) -> str:
    return _bounded_text(event.get("task_type") or event.get("eimemory_task_type"), 200) or default


def _event_id(event: Mapping[str, Any], *names: str, default: str) -> str:
    for name in names:
        value = _bounded_text(event.get(name), 500)
        if value:
            return value
    return default


def _safe_summary(value: Any, *, preview_limit: int) -> str:
    budget = {
        "chars": max(1_024, int(preview_limit) + SUMMARY_REDACTION_SLACK_CHARS),
        "nodes": MAX_SUMMARY_REDACTION_NODES,
    }
    try:
        safe_value = _redact_structured(value, budget=budget)
        redacted = safe_value if isinstance(safe_value, str) else json.dumps(
            safe_value,
            ensure_ascii=False,
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError):
        redacted = f"[UNSERIALIZABLE:{type(value).__name__}]"
    digest = sha256(redacted.encode("utf-8", errors="replace")).hexdigest()
    preview = _bounded_text(redacted, preview_limit)
    return f"sha256={digest}; preview={preview}"


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_structured(
    value: Any,
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> Any:
    remaining = budget if budget is not None else {
        "chars": MAX_TOOL_RESULT_PREVIEW_CHARS + SUMMARY_REDACTION_SLACK_CHARS,
        "nodes": MAX_SUMMARY_REDACTION_NODES,
    }
    if depth >= 16 or remaining["nodes"] <= 0 or remaining["chars"] <= 0:
        return _TRUNCATED
    remaining["nodes"] -= 1
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if remaining["nodes"] <= 0 or remaining["chars"] <= 0:
                redacted[_TRUNCATED] = _TRUNCATED
                break
            key_text = _bounded_redacted_text(str(key), remaining, max_chars=256, redact=False)
            canonical = _KEY_CANONICAL.sub("", key_text.lower())
            sensitive = canonical in _SENSITIVE_KEY_NAMES or _SENSITIVE_KEY_SUFFIX.search(canonical) is not None
            redacted[key_text] = (
                "[REDACTED]"
                if sensitive
                else _redact_structured(item, depth=depth + 1, budget=remaining)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        redacted_items: list[Any] = []
        for item in value:
            if remaining["nodes"] <= 0 or remaining["chars"] <= 0:
                redacted_items.append(_TRUNCATED)
                break
            redacted_items.append(_redact_structured(item, depth=depth + 1, budget=remaining))
        return redacted_items
    if isinstance(value, str):
        return _bounded_redacted_text(value, remaining)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[UNSERIALIZABLE:{type(value).__name__}]"


def _bounded_redacted_text(
    value: str,
    budget: dict[str, int],
    *,
    max_chars: int | None = None,
    redact: bool = True,
) -> str:
    allowed = max(0, budget["chars"])
    if max_chars is not None:
        allowed = min(allowed, max(0, int(max_chars)))
    if allowed <= 0:
        return _TRUNCATED
    scan_limit = min(len(value), allowed + SUMMARY_REDACTION_SLACK_CHARS)
    candidate = value[:scan_limit]
    safe = _redact_text(candidate) if redact else candidate
    truncated = len(value) > scan_limit or len(safe) > allowed
    if truncated:
        keep = max(0, allowed - len(_TRUNCATED))
        safe = safe[:keep] + _TRUNCATED
    budget["chars"] = max(0, budget["chars"] - len(safe))
    return safe


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
        attestation_client=codex_attestation_client_from_env(),
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
