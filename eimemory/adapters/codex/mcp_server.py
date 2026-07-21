from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from eimemory.adapters.codex.hook import codex_client_from_env, codex_scope_from_env


MCP_PROTOCOL_VERSION = "2025-06-18"


class CodexMCPServer:
    def __init__(self, *, client: Any, scope: Mapping[str, str]) -> None:
        self.client = client
        self.scope = dict(scope)

    def handle_message(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "eimemory-codex", "version": "agent.runtime.v1"},
                    "instructions": "Recall before durable decisions; remember only reusable verified knowledge; verify outcomes explicitly.",
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tool_schemas()})
        if method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                result = self._call_tool(name, arguments)
            except (TypeError, ValueError) as exc:
                error = {"ok": False, "error": str(exc)[:300]}
                return self._result(request_id, self._tool_result(error, is_error=True))
            except Exception:
                bypass = {
                    "ok": False,
                    "bypassed": True,
                    "error": "adapter_unavailable",
                    "result": None,
                }
                return self._result(request_id, self._tool_result(bypass, is_error=True))
            return self._result(
                request_id,
                self._tool_result(result, is_error=result.get("ok") is not True),
            )
        return self._error(request_id, -32601, "Method not found")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        common = {"channel": "codex", "scope": dict(self.scope)}
        if name == "eimemory_recall":
            query = _required_text(arguments, "query")
            return self.client.call_or_bypass(
                "adapter.prefetch",
                {
                    **common,
                    "query": query,
                    "task_type": str(arguments.get("task_type") or "code.task"),
                    "limit": max(1, min(50, int(arguments.get("limit", 8)))),
                },
            )
        if name == "eimemory_remember":
            return self.client.call_or_bypass(
                "adapter.remember",
                {
                    **common,
                    "text": _required_text(arguments, "text"),
                    "event_id": _required_text(arguments, "event_id"),
                    "memory_type": str(arguments.get("memory_type") or "durable_fact"),
                    "title": str(arguments.get("title") or "Codex long-term memory"),
                    "force_capture": _optional_bool(arguments, "force_capture", default=False),
                },
            )
        if name == "eimemory_verify_outcome":
            success = arguments.get("success")
            if not isinstance(success, bool):
                raise ValueError("success must be a boolean")
            return self.client.call_or_bypass(
                "adapter.record_terminal",
                {
                    **common,
                    "end_kind": "stop",
                    "session_id": _required_text(arguments, "session_id"),
                    "event_id": _required_text(arguments, "event_id"),
                    "task_type": _required_text(arguments, "task_type"),
                    "success": success,
                    "verification": _required_text(arguments, "verification"),
                    "result": str(arguments.get("result") or "")[:2_000],
                    "tool_receipts": _optional_receipts(arguments),
                    "rehearsal": False,
                },
            )
        if name == "eimemory_status":
            return self.client.call_or_bypass("adapter.status", common)
        raise ValueError("unknown eimemory tool")

    @staticmethod
    def _tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": bool(is_error),
        }

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_schemas() -> list[dict[str, Any]]:
        return [
            {
                "name": "eimemory_recall",
                "description": "Recall authoritative long-term memory from the independent Codex channel.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "task_type": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "eimemory_remember",
                "description": "Write reusable accepted knowledge to authoritative Codex long-term memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "event_id": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "title": {"type": "string"},
                        "force_capture": {"type": "boolean"},
                    },
                    "required": ["text", "event_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "eimemory_verify_outcome",
                "description": "Record an explicitly verified Codex task outcome bound to release evidence.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "event_id": {"type": "string"},
                        "task_type": {"type": "string"},
                        "success": {"type": "boolean"},
                        "verification": {"type": "string"},
                        "result": {"type": "string"},
                        "tool_receipts": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["session_id", "event_id", "task_type", "success", "verification"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "eimemory_status",
                "description": "Check Codex channel scope, authority mode, runtime health, and release binding.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_bool(arguments: Mapping[str, Any], name: str, *, default: bool) -> bool:
    if name not in arguments:
        return default
    value = arguments.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_receipts(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = arguments.get("tool_receipts")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("tool_receipts must be an array of objects")
    return list(value[:32])


def run_stdio(*, stdin: Any = None, stdout: Any = None) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    server = CodexMCPServer(client=codex_client_from_env(), scope=codex_scope_from_env())
    for line in input_stream:
        try:
            message = json.loads(line)
            response = server.handle_message(message if isinstance(message, dict) else {})
        except json.JSONDecodeError:
            response = server._error(None, -32700, "Parse error")
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    del argv
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
