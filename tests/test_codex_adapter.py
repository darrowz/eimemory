from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from eimemory.adapters.codex import hook as codex_hook
from eimemory.adapters.codex import mcp_server as codex_mcp
from eimemory.adapters.codex.hook import CodexHookAdapter
from eimemory.adapters.codex.mcp_server import CodexMCPServer


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_or_bypass(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "adapter.prefetch":
            return {
                "ok": True,
                "bypassed": False,
                "result": {"context": "Relevant eimemory context:\n- [memory] durable rule"},
            }
        return {"ok": True, "bypassed": False, "result": {"stored": True}}


def test_user_prompt_submit_prefetches_bounded_context_without_reading_transcript(monkeypatch) -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    def forbid_transcript_read(*args, **kwargs):
        raise AssertionError("Codex transcripts must never be read")

    monkeypatch.setattr(Path, "read_text", forbid_transcript_read)
    result = adapter.handle(
        "UserPromptSubmit",
        {
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "prompt": "Fix the memory recall regression and run tests.",
            "transcript_path": "C:/private/codex-session.jsonl",
            "cwd": "E:/eimemory",
        },
    )

    assert client.calls[0][0] == "adapter.prefetch"
    assert client.calls[0][1]["channel"] == "codex"
    assert client.calls[0][1]["query"] == "Fix the memory recall regression and run tests."
    assert result["continue"] is True
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "durable rule" in result["hookSpecificOutput"]["additionalContext"]
    assert len(result["hookSpecificOutput"]["additionalContext"]) <= 7_200


def test_post_tool_use_hashes_redacts_and_truncates_before_sync() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)
    secret = "sk-secret-that-must-not-leave-the-hook"
    huge_result = "x" * 20_000 + secret

    result = adapter.handle(
        "PostToolUse",
        {
            "session_id": "codex-session",
            "turn_id": "turn-2",
            "tool_name": "Bash",
            "tool_call_id": "call-2",
            "tool_input": {"command": f"run --token {secret}"},
            "tool_response": huge_result,
        },
    )

    method, params = client.calls[0]
    forwarded = json.dumps(params, ensure_ascii=False)
    assert method == "adapter.sync_turn"
    assert params["channel"] == "codex"
    assert "input_sha256=" in params["user_text"]
    assert "result_sha256=" in params["assistant_text"]
    assert secret not in forwarded
    assert huge_result not in forwarded
    assert len(params["user_text"]) <= 2_500
    assert len(params["assistant_text"]) <= 4_500
    assert result == {"continue": True}


def test_stop_never_blocks_and_does_not_invent_verification() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    result = adapter.handle(
        "Stop",
        {
            "session_id": "codex-session",
            "turn_id": "turn-3",
            "task_type": "code.fix",
            "success": True,
            "last_assistant_message": "Implemented the requested fix.",
        },
    )

    method, params = client.calls[-1]
    assert method == "adapter.record_terminal"
    assert params["end_kind"] == "stop"
    assert params["verification"] == ""
    assert params["success"] is True
    assert result["continue"] is True
    assert "stopReason" not in result


def test_hook_transport_failure_is_advisory_and_silent() -> None:
    class BypassClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}

    adapter = CodexHookAdapter(client=BypassClient(), scope=BASE_SCOPE)

    assert adapter.handle("SessionStart", {"session_id": "s", "cwd": "E:/repo"}) == {"continue": True}
    assert adapter.handle("Stop", {"session_id": "s", "turn_id": "t"}) == {"continue": True}


def test_codex_mcp_exposes_only_closed_loop_memory_tools() -> None:
    client = FakeClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)

    listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == [
        "eimemory_recall",
        "eimemory_remember",
        "eimemory_verify_outcome",
        "eimemory_status",
    ]

    called = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "eimemory_verify_outcome",
                "arguments": {
                    "session_id": "codex-session",
                    "event_id": "turn-verified",
                    "task_type": "code.test",
                    "success": True,
                    "verification": "pytest tests/test_codex_adapter.py: passed",
                    "result": "all adapter tests passed",
                },
            },
        }
    )

    method, params = client.calls[-1]
    assert method == "adapter.record_terminal"
    assert params["channel"] == "codex"
    assert params["end_kind"] == "stop"
    assert params["verification"].startswith("pytest")
    assert called["result"]["isError"] is False


def test_codex_hook_and_mcp_stdio_framing(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(codex_hook, "codex_client_from_env", lambda: client)
    monkeypatch.setattr(codex_mcp, "codex_client_from_env", lambda: client)

    hook_output = StringIO()
    exit_code = codex_hook.run_hook_from_stdio(
        "UserPromptSubmit",
        stdin=StringIO(
            json.dumps(
                {
                    "session_id": "codex-session",
                    "turn_id": "turn-stdio",
                    "prompt": "Recall the release contract",
                    "cwd": "E:/eimemory",
                }
            )
        ),
        stdout=hook_output,
    )
    hook_result = json.loads(hook_output.getvalue())

    mcp_output = StringIO()
    codex_mcp.run_stdio(
        stdin=StringIO(
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                ]
            )
            + "\n"
        ),
        stdout=mcp_output,
    )
    responses = [json.loads(line) for line in mcp_output.getvalue().splitlines()]

    assert exit_code == 0
    assert hook_result["continue"] is True
    assert responses[0]["result"]["serverInfo"]["name"] == "eimemory-codex"
    assert len(responses[1]["result"]["tools"]) == 4
