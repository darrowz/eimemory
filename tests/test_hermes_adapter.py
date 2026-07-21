from __future__ import annotations

import json
from pathlib import Path
import threading

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_or_bypass(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "adapter.prefetch":
            return {
                "ok": True,
                "bypassed": False,
                "result": {"context": "Relevant eimemory context:\n- [preference] cite primary sources"},
            }
        if method == "adapter.status":
            return {
                "ok": True,
                "bypassed": False,
                "result": {"authority_mode": "per_channel", "channel": "hermes"},
            }
        return {"ok": True, "bypassed": False, "result": {"stored": True}}


def test_hermes_provider_lifecycle_is_channel_local_and_flushes_bounded_writes(tmp_path: Path) -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize(
        "hermes-session",
        hermes_home=str(tmp_path),
        agent_identity="researcher",
        agent_workspace="embodied",
        user_id="darrow",
        agent_context="primary",
    )

    context = provider.prefetch("How should this research conclusion be sourced?")
    provider.sync_turn(
        "Remember to cite primary sources.",
        "Stored as a durable preference.",
        session_id="hermes-session",
    )
    provider.on_memory_write(
        "add",
        "memory",
        "Hermes must cite primary evidence for durable research conclusions.",
        {"session_id": "hermes-session", "event_id": "memory-write-1"},
    )
    provider.shutdown()

    assert provider.name == "eimemory"
    assert "cite primary sources" in context
    assert provider.background_worker_count == 0
    write_calls = [(method, params) for method, params in client.calls if method in {"adapter.sync_turn", "adapter.remember"}]
    assert {method for method, _ in write_calls} == {"adapter.sync_turn", "adapter.remember"}
    assert all(params["channel"] == "hermes" for _, params in client.calls)
    assert all(params["scope"]["workspace_id"] == "embodied" for _, params in client.calls)


def test_hermes_provider_exposes_only_closed_loop_tools_and_verified_terminal() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    names = [schema["name"] for schema in provider.get_tool_schemas()]
    output = json.loads(
        provider.handle_tool_call(
            "eimemory_verify_outcome",
            {
                "session_id": "hermes-session",
                "event_id": "hermes-task-1",
                "task_type": "research.audit",
                "success": True,
                "verification": "primary-source-check:passed",
                "result": "audit complete",
            },
        )
    )

    method, params = client.calls[-1]
    assert names == [
        "eimemory_recall",
        "eimemory_remember",
        "eimemory_verify_outcome",
        "eimemory_status",
    ]
    assert method == "adapter.record_terminal"
    assert params["end_kind"] == "task_end"
    assert params["verification"] == "primary-source-check:passed"
    assert output["ok"] is True


def test_hermes_unknown_tool_error_identifies_rejected_name() -> None:
    provider = HermesMemoryProviderCore(client=FakeClient())
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    output = json.loads(provider.handle_tool_call("eimemory_stale_tool", {}))

    assert output == {"ok": False, "error": "unknown eimemory tool: 'eimemory_stale_tool'"}


def test_hermes_lifecycle_ignores_full_history_and_session_end_cannot_count_as_l5() -> None:
    class ForbiddenHistory(list):
        def __iter__(self):
            raise AssertionError("Hermes full history must not be read")

    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    provider.on_session_end(ForbiddenHistory([{"role": "user", "content": "private transcript"}]))

    method, params = client.calls[-1]
    assert method == "adapter.record_terminal"
    assert params["end_kind"] == "session_end"
    assert params["success"] is None
    assert params["verification"] == ""


def test_hermes_provider_skips_non_primary_writes() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("cron-session", agent_workspace="embodied", agent_context="cron")

    provider.sync_turn("user", "assistant", session_id="cron-session")
    provider.on_memory_write("add", "memory", "must not write")
    provider.on_session_end([])
    provider.shutdown()

    written_methods = {"adapter.sync_turn", "adapter.remember", "adapter.record_terminal"}
    assert not any(method in written_methods for method, _ in client.calls)


def test_hermes_provider_is_fail_open_when_primary_rpc_is_unavailable() -> None:
    class RaisingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def call_or_bypass(self, method: str, params: dict) -> dict:
            self.calls.append(method)
            raise OSError("offline")

    client = RaisingClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("primary-session", agent_workspace="embodied", agent_context="primary")

    assert provider.prefetch("safe bypass") == ""
    provider.sync_turn("user", "assistant", session_id="primary-session")
    provider.on_memory_write("add", "memory", "bounded write")
    provider.shutdown()

    assert "adapter.prefetch" in client.calls
    assert "adapter.sync_turn" in client.calls
    assert "adapter.remember" in client.calls


def test_hermes_prefetch_queue_is_single_worker_and_cache_bounded() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client, max_prefetch_cache_entries=3)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    for index in range(8):
        provider.queue_prefetch(f"query-{index}", session_id="hermes-session")
    provider.shutdown()

    assert provider.prefetch_cache_size <= 3
    assert provider.background_worker_count == 0
    prefetch_queries = [params["query"] for method, params in client.calls if method == "adapter.prefetch"]
    assert "query-7" in prefetch_queries


def test_hermes_prefetch_does_not_cache_transient_bypass() -> None:
    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def call_or_bypass(self, method: str, params: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}
            return {
                "ok": True,
                "bypassed": False,
                "result": {"context": "recovered Hermes context"},
            }

    client = FlakyClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    assert provider.prefetch("same query") == ""
    assert provider.prefetch("same query") == "recovered Hermes context"
    assert client.calls == 2


def test_hermes_pre_compress_uses_latest_bounded_turn_not_stale_recall() -> None:
    class ForbiddenHistory(list):
        def __iter__(self):
            raise AssertionError("Hermes full history must not be read")

    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.prefetch("unrelated old query")
    provider.sync_turn("current bounded user turn", "current bounded assistant turn")
    provider.shutdown()

    snapshot = provider.on_pre_compress(ForbiddenHistory([{"role": "user", "content": "private"}]))

    assert "current bounded user turn" in snapshot
    assert "current bounded assistant turn" in snapshot
    assert "cite primary sources" not in snapshot
    assert len(snapshot) <= 2_000


def test_hermes_delegation_uses_bounded_turn_sync_without_full_history() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("parent-session", agent_workspace="embodied", agent_context="primary")

    provider.on_delegation(
        "Audit the release evidence",
        "Verified deployment receipt and L5 readiness",
        child_session_id="child-session",
    )
    provider.shutdown()

    sync_calls = [params for method, params in client.calls if method == "adapter.sync_turn"]
    assert len(sync_calls) == 1
    assert sync_calls[0]["session_id"] == "child-session"
    assert "Audit the release evidence" in sync_calls[0]["user_text"]
    assert "Verified deployment receipt" in sync_calls[0]["assistant_text"]


def test_hermes_default_loopback_url_is_available_with_token_only(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_RPC_URL", raising=False)
    monkeypatch.setenv("EIMEMORY_RPC_TOKEN", "HermesRuntimeAdapterToken_0123456789-Strong")
    provider = HermesMemoryProviderCore()

    assert provider.is_available() is True
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    assert "authority_mode=per_channel" in provider.system_prompt_block()


def test_hermes_bounded_queue_reports_dropped_writes() -> None:
    class BlockingClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls: list[tuple[str, dict]] = []

        def call_or_bypass(self, method: str, params: dict) -> dict:
            self.calls.append((method, params))
            if method == "adapter.sync_turn" and not self.started.is_set():
                self.started.set()
                self.release.wait(timeout=5.0)
            return {"ok": True, "bypassed": False, "result": {}}

    client = BlockingClient()
    provider = HermesMemoryProviderCore(client=client, max_write_queue=1)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.sync_turn("turn one", "assistant one")
    assert client.started.wait(timeout=2.0)
    provider.sync_turn("turn two", "assistant two")
    provider.sync_turn("turn three", "assistant three")

    assert provider.dropped_write_count == 1
    status = json.loads(provider.handle_tool_call("eimemory_status", {}))
    assert status["adapter_local"]["dropped_writes"] == 1
    client.release.set()
    provider.shutdown()
