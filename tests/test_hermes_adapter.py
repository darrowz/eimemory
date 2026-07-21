from __future__ import annotations

import json
from pathlib import Path

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


def test_hermes_provider_is_fail_open_and_skips_non_primary_writes() -> None:
    class RaisingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def call_or_bypass(self, method: str, params: dict) -> dict:
            self.calls.append(method)
            raise OSError("offline")

    client = RaisingClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("cron-session", agent_workspace="embodied", agent_context="cron")

    assert provider.prefetch("safe bypass") == ""
    provider.sync_turn("user", "assistant", session_id="cron-session")
    provider.on_memory_write("add", "memory", "must not write")
    provider.on_session_end([])
    provider.shutdown()

    assert "adapter.prefetch" in client.calls
    assert "adapter.sync_turn" not in client.calls
    assert "adapter.remember" not in client.calls
    assert "adapter.record_terminal" not in client.calls


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
