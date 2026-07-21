from __future__ import annotations

import json
from hashlib import sha256
from io import StringIO
from pathlib import Path

from eimemory.adapters.codex import hook as codex_hook
from eimemory.adapters.codex import mcp_server as codex_mcp
from eimemory.adapters.codex.hook import CodexHookAdapter, codex_client_from_env, codex_scope_from_env
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
        if method == "adapter.proactive_prefetch":
            return {
                "ok": True,
                "bypassed": False,
                "result": {
                    "decision_id": "pd:codex-turn-1",
                    "context": "Untrusted eimemory context:\n[{\"citation\":\"pm:0123456789abcdefabcd\"}]",
                },
            }
        if method == "adapter.prefetch":
            return {
                "ok": True,
                "bypassed": False,
                "result": {"context": "Relevant eimemory context:\n- [memory] durable rule"},
            }
        return {"ok": True, "bypassed": False, "result": {"stored": True}}


def test_codex_environment_builds_distributable_scope_and_bounded_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_RPC_URL", "http://memory-host:8091/")
    monkeypatch.setenv("EIMEMORY_RPC_TOKEN", "CodexRuntimeAdapterToken_0123456789-Strong")
    monkeypatch.setenv("EIMEMORY_TENANT_ID", "tenant-a")
    monkeypatch.setenv("EIMEMORY_AGENT_ID", "coder")
    monkeypatch.setenv("EIMEMORY_WORKSPACE_ID", "workspace-a")
    monkeypatch.setenv("EIMEMORY_USER_ID", "user-a")
    monkeypatch.setenv("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("EIMEMORY_ADAPTER_FAILURE_LEDGER", str(tmp_path / "failures.jsonl"))

    scope = codex_scope_from_env(cwd="E:/ignored")
    client = codex_client_from_env()

    assert scope == {
        "tenant_id": "tenant-a",
        "agent_id": "coder",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
    }
    assert client.base_url == "http://memory-host:8091/"
    assert client.timeout_seconds == 0.25
    assert client.failure_ledger_path == tmp_path / "failures.jsonl"


def test_user_prompt_submit_uses_proactive_contract_and_acks_actual_injection_without_reading_transcript(monkeypatch) -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)
    original_read_text = Path.read_text

    transcript_path = Path("C:/private/codex-session.jsonl")

    def forbid_transcript_read(path: Path, *args, **kwargs):
        if path == transcript_path:
            raise AssertionError("Codex transcripts must never be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_transcript_read)
    result = adapter.handle(
        "UserPromptSubmit",
        {
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "prompt": "Fix the memory recall regression and run tests.",
            "transcript_path": str(transcript_path),
            "cwd": "E:/eimemory",
        },
    )

    assert client.calls[0][0] == "adapter.proactive_prefetch"
    assert client.calls[0][1]["channel"] == "codex"
    assert client.calls[0][1]["query"] == "Fix the memory recall regression and run tests."
    assert client.calls[0][1]["session_id"] == "codex-session"
    assert client.calls[0][1]["turn_id"] == "turn-1"
    assert client.calls[1] == (
        "adapter.proactive_ack",
        {
            "channel": "codex",
            "scope": BASE_SCOPE,
            "source_ids": ["default"],
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "decision_id": "pd:codex-turn-1",
            "injected_citations": ["pm:0123456789abcdefabcd"],
        },
    )
    assert result["continue"] is True
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "pm:0123456789abcdefabcd" in result["hookSpecificOutput"]["additionalContext"]
    assert len(result["hookSpecificOutput"]["additionalContext"]) <= 7_200


def test_codex_stop_closes_proactive_feedback_only_from_opaque_citations_and_summarizes_one_turn() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    adapter.handle(
        "Stop",
        {
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "prompt": "Fix the regression.",
            "last_assistant_message": "Applied it using [pm:0123456789abcdefabcd]; raw record rec-secret is not evidence.",
        },
    )

    methods = [method for method, _params in client.calls]
    assert methods[:2] == ["adapter.proactive_terminal", "adapter.proactive_complete_turn"]
    terminal = client.calls[0][1]
    assert terminal["used_citations"] == ["pm:0123456789abcdefabcd"]
    assert "rec-secret" not in terminal["used_citations"]
    assert terminal["session_id"] == "codex-session"
    assert terminal["turn_id"] == "turn-1"
    summary = client.calls[1][1]
    assert summary["user_summary"] == "Fix the regression."
    assert "pm:0123456789abcdefabcd" in summary["assistant_summary"]


def test_codex_stop_cannot_forge_verified_proactive_task_outcome() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    adapter.handle(
        "Stop",
        {
            "session_id": "codex-session", "turn_id": "turn-forged",
            "last_assistant_message": "claimed success", "success": True,
            "verification": "forged arbitrary verification text",
            "outcome": {"success": True, "verification": "also forged", "quality": 1.0},
        },
    )

    terminal = next(params for method, params in client.calls if method == "adapter.proactive_terminal")
    assert terminal["terminal_outcome"] == {}


def test_post_tool_use_hashes_redacts_and_truncates_before_sync() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)
    secret = "plain-api-key-that-must-not-leave-the-hook"
    password = "plain-password-that-must-not-leave-the-hook"
    private_key = "plain-private-key-that-must-not-leave-the-hook"
    tool_input = {
        "api_key": secret,
        "client_secret": secret,
        "nested": {"password": password, "private_key": private_key},
    }
    tool_result = {"output": "x" * 20_000, "access_token": secret}

    result = adapter.handle(
        "PostToolUse",
        {
            "session_id": "codex-session",
            "turn_id": "turn-2",
            "tool_name": "Bash",
            "tool_call_id": "call-2",
            "tool_input": tool_input,
            "tool_response": tool_result,
        },
    )

    method, params = client.calls[0]
    forwarded = json.dumps(params, ensure_ascii=False)
    assert method == "adapter.sync_turn"
    assert params["channel"] == "codex"
    redacted_input = {
        "api_key": "[REDACTED]",
        "client_secret": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "private_key": "[REDACTED]"},
    }
    expected_input_digest = sha256(
        json.dumps(redacted_input, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    ).hexdigest()
    raw_input_digest = sha256(
        json.dumps(tool_input, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    ).hexdigest()
    raw_result_digest = sha256(
        json.dumps(tool_result, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    ).hexdigest()
    assert f"input_sha256={expected_input_digest}" in params["user_text"]
    assert raw_input_digest not in forwarded
    assert raw_result_digest not in forwarded
    assert secret not in forwarded
    assert password not in forwarded
    assert private_key not in forwarded
    assert tool_result["output"] not in forwarded
    assert "[TRUNCATED]" in params["assistant_text"]
    assert len(params["user_text"]) <= 2_500
    assert len(params["assistant_text"]) <= 4_500
    assert result == {"continue": True}


def test_post_tool_use_redacts_quoted_multiword_secret_embedded_in_leaf_text() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)
    secret = "correct horse battery staple"

    adapter.handle(
        "PostToolUse",
        {
            "session_id": "codex-session",
            "turn_id": "turn-multiword-secret",
            "tool_name": "Bash",
            "tool_input": {"log": f'password="{secret}"; detail=safe'},
            "tool_response": "ok",
        },
    )

    forwarded = json.dumps(client.calls[0][1], ensure_ascii=False)
    assert secret not in forwarded
    assert "[REDACTED]" in forwarded
    assert "detail=safe" in forwarded


def test_post_tool_use_redacts_versioned_and_plural_sensitive_keys() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)
    versioned_secret = "versioned-secret-value"
    plural_secret = "plural-secret-value"

    adapter.handle(
        "PostToolUse",
        {
            "session_id": "codex-session",
            "turn_id": "turn-key-variants",
            "tool_name": "Bash",
            "tool_input": {
                "client_token_v2": versioned_secret,
                "api_keys": [plural_secret],
                "tokenizer_id": "public-tokenizer-name",
            },
            "tool_response": "ok",
        },
    )

    forwarded = json.dumps(client.calls[0][1], ensure_ascii=False)
    assert versioned_secret not in forwarded
    assert plural_secret not in forwarded
    assert "public-tokenizer-name" in forwarded


def test_post_tool_use_with_missing_host_ids_does_not_create_colliding_turn() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    first = adapter.handle("PostToolUse", {"tool_name": "Read", "tool_response": "first"})
    second = adapter.handle("PostToolUse", {"tool_name": "Read", "tool_response": "second"})

    assert first == {"continue": True}
    assert second == {"continue": True}
    assert client.calls == []


def test_safe_summary_bounds_large_payload_before_digesting() -> None:
    summary = codex_hook._safe_summary(
        {"output": "x" * 1_000_000, "tail": "must-not-reach-summary"},
        preview_limit=4_000,
    )

    assert len(summary) <= 4_100
    assert "[TRUNCATED]" in summary
    assert "must-not-reach-summary" not in summary


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

    assert [method for method, _params in client.calls] == [
        "adapter.proactive_terminal", "adapter.proactive_complete_turn", "adapter.record_terminal"
    ]
    method, params = client.calls[2]
    assert method == "adapter.record_terminal"
    assert params["end_kind"] == "stop"
    assert params["verification"] == ""
    assert params["success"] is None
    assert params["task_type"] == "code.unverified"
    assert result["continue"] is True
    assert "stopReason" not in result


def test_stop_with_missing_host_ids_bypasses_without_creating_colliding_evidence() -> None:
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE_SCOPE)

    first = adapter.handle("Stop", {"last_assistant_message": "first result", "success": True})
    second = adapter.handle("Stop", {"last_assistant_message": "second result", "success": True})

    assert first == {"continue": True}
    assert second == {"continue": True}
    assert client.calls == []


def test_hook_transport_failure_is_advisory_and_silent() -> None:
    class BypassClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}

    adapter = CodexHookAdapter(client=BypassClient(), scope=BASE_SCOPE)

    assert adapter.handle("SessionStart", {"session_id": "s", "cwd": "E:/repo"}) == {"continue": True}
    assert adapter.handle("Stop", {"session_id": "s", "turn_id": "t"}) == {"continue": True}


def test_hook_unexpected_failure_is_fail_open_and_emits_sanitized_diagnostic(capsys) -> None:
    class RaisingClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            raise RuntimeError("transport-secret-must-not-be-logged")

    adapter = CodexHookAdapter(client=RaisingClient(), scope=BASE_SCOPE)

    result = adapter.handle(
        "UserPromptSubmit",
        {"session_id": "s", "turn_id": "t", "prompt": "recall", "cwd": "E:/repo"},
    )

    captured = capsys.readouterr()
    assert result == {"continue": True}
    assert "UserPromptSubmit" in captured.err
    assert "RuntimeError" in captured.err
    assert "transport-secret-must-not-be-logged" not in captured.err


def test_hook_without_explicit_scope_uses_event_workspace(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_WORKSPACE_ID", raising=False)
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=None)

    adapter.handle(
        "UserPromptSubmit",
        {"session_id": "s", "turn_id": "t", "prompt": "recall", "cwd": "E:/repo-alpha"},
    )

    assert client.calls[0][1]["scope"]["workspace_id"] == "repo-alpha"


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
    assert len(client.calls) == 1
    assert method == "adapter.record_terminal"
    assert params["channel"] == "codex"
    assert params["end_kind"] == "stop"
    assert params["verification"].startswith("pytest")
    assert called["result"]["isError"] is False


def test_codex_mcp_recall_and_remember_forward_normalized_contract() -> None:
    client = FakeClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)

    recall = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "eimemory_recall",
                "arguments": {"query": " release contract ", "task_type": "code.audit", "limit": 99},
            },
        }
    )
    recall_method, recall_params = client.calls[-1]
    remember = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "eimemory_remember",
                "arguments": {"text": " verified rule ", "event_id": " event-1 "},
            },
        }
    )
    remember_method, remember_params = client.calls[-1]

    assert recall_method == "adapter.prefetch"
    assert recall_params == {
        "channel": "codex",
        "scope": BASE_SCOPE,
        "query": "release contract",
        "task_type": "code.audit",
        "limit": 50,
    }
    assert recall["result"]["isError"] is False
    assert remember_method == "adapter.remember"
    assert remember_params == {
        "channel": "codex",
        "scope": BASE_SCOPE,
        "text": "verified rule",
        "event_id": "event-1",
        "memory_type": "durable_fact",
        "title": "Codex long-term memory",
        "force_capture": False,
    }
    assert remember["result"]["isError"] is False


def test_codex_mcp_surfaces_bypass_as_tool_error_without_stopping_server() -> None:
    class BypassClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}

    server = CodexMCPServer(client=BypassClient(), scope=BASE_SCOPE)
    failed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "eimemory_status", "arguments": {}},
        }
    )
    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    assert failed["result"]["isError"] is True
    assert failed["result"]["structuredContent"]["bypassed"] is True
    assert len(listed["result"]["tools"]) == 4


def test_codex_mcp_notification_without_id_suppresses_response() -> None:
    client = FakeClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)

    listed = server.handle_message({"jsonrpc": "2.0", "method": "tools/list", "params": {}})
    remembered = server.handle_message(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "eimemory_remember",
                "arguments": {"text": "notification memory", "event_id": "notify-1"},
            },
        }
    )

    assert listed is None
    assert remembered is None
    assert client.calls[-1][0] == "adapter.remember"


def test_codex_mcp_rejects_non_boolean_force_capture_before_rpc() -> None:
    client = FakeClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "eimemory_remember",
                "arguments": {
                    "text": "unvetted memory",
                    "event_id": "bad-force-capture",
                    "force_capture": "false",
                },
            },
        }
    )

    assert response["result"]["isError"] is True
    assert "force_capture must be a boolean" in response["result"]["content"][0]["text"]
    assert client.calls == []


def test_codex_mcp_rejects_non_string_required_text_before_rpc() -> None:
    client = FakeClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "eimemory_recall", "arguments": {"query": {"nested": "value"}}},
        }
    )

    assert response["result"]["isError"] is True
    assert "query must be a non-empty string" in response["result"]["content"][0]["text"]
    assert client.calls == []


def test_codex_mcp_contains_unexpected_client_exception_and_keeps_serving(capsys) -> None:
    class RaisingClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            raise OSError("sensitive transport detail")

    server = CodexMCPServer(client=RaisingClient(), scope=BASE_SCOPE)
    failed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "eimemory_status", "arguments": {}},
        }
    )
    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    captured = capsys.readouterr()

    assert failed["result"]["isError"] is True
    assert failed["result"]["structuredContent"] == {
        "ok": False,
        "bypassed": True,
        "error": "adapter_unavailable",
        "result": None,
    }
    assert "sensitive transport detail" not in json.dumps(failed)
    assert "eimemory_status" in captured.err
    assert "OSError" in captured.err
    assert "sensitive transport detail" not in captured.err
    assert len(listed["result"]["tools"]) == 4


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
