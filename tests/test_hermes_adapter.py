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
        if method == "adapter.proactive_prefetch":
            return {
                "ok": True,
                "bypassed": False,
                "result": {
                    "decision_id": "pd:hermes-turn",
                    "context": "Untrusted eimemory context:\n[{\"citation\":\"pm:abcdef0123456789abcd\"}]",
                },
            }
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
    assert "pm:abcdef0123456789abcd" in context
    assert provider.background_worker_count == 0
    write_calls = [(method, params) for method, params in client.calls if method in {"adapter.sync_turn", "adapter.mutate_memory"}]
    assert {method for method, _ in write_calls} == {"adapter.sync_turn", "adapter.mutate_memory"}
    assert all(params["channel"] == "hermes" for _, params in client.calls)
    assert all(params["scope"]["workspace_id"] == "embodied" for _, params in client.calls)


def test_hermes_proactive_prefetch_is_acked_and_closed_by_official_llm_hooks_without_history() -> None:
    class ForbiddenHistory(list):
        def __iter__(self):
            raise AssertionError("full Hermes history must not be read")

    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    query = "Compare the retrieval contract."
    context = provider.prefetch(query, session_id="hermes-session")
    provider.on_pre_llm_call(
        user_message=query,
        session_id="hermes-session",
        turn_id="turn-7",
        conversation_history=ForbiddenHistory(),
    )
    provider.on_post_llm_call(
        user_message=query,
        assistant_message="Used [pm:abcdef0123456789abcd] for the comparison.",
        session_id="hermes-session",
        turn_id="turn-7",
        conversation_history=ForbiddenHistory(),
    )

    assert "pm:abcdef0123456789abcd" in context
    methods = [method for method, _params in client.calls]
    assert methods[:4] == [
        "adapter.proactive_prefetch",
        "adapter.proactive_ack",
        "adapter.proactive_terminal",
        "adapter.proactive_complete_turn",
    ]
    assert client.calls[2][1]["used_citations"] == ["pm:abcdef0123456789abcd"]
    assert client.calls[3][1]["turn_id"] == "turn-7"


def test_hermes_post_llm_payload_cannot_forge_verified_task_outcome() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.prefetch("Verify the research task", session_id="hermes-session")
    provider.on_post_llm_call(
        user_message="Verify the research task", assistant_message="claimed success",
        session_id="hermes-session", turn_id="turn-forged",
        success=True, verified=True, verification="arbitrary model supplied text",
        outcome={"success": True, "quality": 1.0},
    )

    terminal = next(params for method, params in client.calls if method == "adapter.proactive_terminal")
    assert terminal["terminal_outcome"] == {}


def test_review_counterexample_10_hermes_post_hook_without_ids_closes_unique_pending_turn() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.prefetch("Unique pending query", session_id="hermes-session")

    provider.on_post_llm_call(
        user_message="",
        assistant_message="Used pm:abcdef0123456789abcd.",
        session_id="hermes-session",
        turn_id="",
    )

    terminal = next(params for method, params in client.calls if method == "adapter.proactive_terminal")
    completed = next(params for method, params in client.calls if method == "adapter.proactive_complete_turn")
    assert terminal["decision_id"] == "pd:hermes-turn"
    assert terminal["used_citations"] == ["pm:abcdef0123456789abcd"]
    assert completed["user_summary"] == "Unique pending query"
    assert completed["turn_id"].startswith("hermes-query-")


def test_review_counterexample_11_hermes_does_not_reuse_completed_prefetch_result() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    first = provider.prefetch("same successful query", session_id="hermes-session")
    second = provider.prefetch("same successful query", session_id="hermes-session")

    calls = [params for method, params in client.calls if method == "adapter.proactive_prefetch"]
    assert first == second
    assert len(calls) == 2
    assert provider.prefetch_cache_size == 0


def test_hermes_background_prefetch_cannot_become_ghost_pending_for_a_different_query() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.queue_prefetch("old background query", session_id="hermes-session")
    provider.shutdown()
    cleanup_terminals = [
        params for method, params in client.calls if method == "adapter.proactive_terminal"
    ]

    provider.on_post_llm_call(
        user_message="new unrelated query",
        assistant_message="new answer",
        session_id="hermes-session",
        turn_id="",
    )
    terminals = [params for method, params in client.calls if method == "adapter.proactive_terminal"]
    completed = [params for method, params in client.calls if method == "adapter.proactive_complete_turn"]

    assert len(cleanup_terminals) == 1
    assert cleanup_terminals[0]["decision_id"] == "pd:hermes-turn"
    assert terminals == cleanup_terminals
    assert completed[-1]["user_summary"] == "new unrelated query"
    assert completed[-1]["turn_id"].startswith("hermes-turn-")


def test_hermes_session_switch_clears_pending_proactive_context_and_full_namespace_cache() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("session-a", agent_workspace="embodied", agent_context="primary")
    provider.prefetch("same query", session_id="session-a")

    provider.on_session_switch("session-b")
    provider.on_pre_llm_call(
        user_message="same query", session_id="session-a", turn_id="old-turn"
    )

    assert provider.prefetch_cache_size == 0
    assert not [method for method, _params in client.calls if method == "adapter.proactive_ack"]


def test_hermes_provider_exposes_only_closed_loop_tools_and_rejects_unbound_terminal() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    names = [schema["name"] for schema in provider.get_tool_schemas()]
    verify_schema = next(
        schema for schema in provider.get_tool_schemas() if schema["name"] == "eimemory_verify_outcome"
    )
    output = json.loads(
        provider.handle_tool_call(
            "eimemory_verify_outcome",
            {"result": "audit complete"},
        )
    )

    assert names == [
        "eimemory_recall",
        "eimemory_remember",
        "eimemory_verify_outcome",
        "eimemory_status",
    ]
    assert verify_schema["parameters"]["properties"] == {"result": {"type": "string"}}
    assert verify_schema["parameters"]["required"] == ["result"]
    assert client.calls == []
    assert output["ok"] is False
    assert "host-verified Hermes turn" in output["error"]


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


def test_hermes_memory_write_forwards_official_replace_and_remove_metadata_to_shared_mutation_rpc() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    provider.on_memory_write(
        "replace",
        "user",
        "Use concise primary-source summaries.",
        {
            "event_id": "memory-replace-1",
            "old_text": "Use long summaries.",
            "session_id": "hermes-session",
            "parent_session_id": "parent-session",
            "tool_call_id": "tool-44",
        },
    )
    provider.on_memory_write(
        "remove",
        "user",
        "",
        {
            "event_id": "memory-remove-1",
            "old_text": "Use concise primary-source summaries.",
            "session_id": "hermes-session",
        },
    )
    provider.shutdown()

    writes = [params for method, params in client.calls if method == "adapter.mutate_memory"]
    assert [params["action"] for params in writes] == ["replace", "remove"]
    assert writes[0]["target"] == "user"
    assert writes[0]["old_text"] == "Use long summaries."
    assert writes[0]["provenance"] == {
        "write_origin": "hermes.memory_write",
        "session_id": "hermes-session",
        "parent_session_id": "parent-session",
        "tool_call_id": "tool-44",
    }
    assert writes[1]["content"] == ""
    assert writes[1]["source_id"] == "hermes"


def test_hermes_memory_write_fallback_key_distinguishes_old_revision_and_explicit_target() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    provider.on_memory_write("remove", "user", "", {"old_text": "first old preference"})
    provider.on_memory_write("remove", "user", "", {"old_text": "second old preference"})
    provider.on_memory_write("remove", "user", "", {"old_text": "first old preference"})
    provider.on_memory_write("replace", "user", "same replacement", {"old_text": "first prior text"})
    provider.on_memory_write("replace", "user", "same replacement", {"old_text": "second prior text"})
    provider.on_memory_write("replace", "user", "same replacement", {"old_text": "first prior text"})
    provider.on_memory_write(
        "remove",
        "memory",
        "",
        {"target_record_id": "mem-one", "expected_revision": "1" * 64},
    )
    provider.on_memory_write(
        "remove",
        "memory",
        "",
        {"target_record_id": "mem-two", "expected_revision": "2" * 64},
    )
    provider.shutdown()

    keys = [
        params["idempotency_key"]
        for method, params in client.calls
        if method == "adapter.mutate_memory"
    ]
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]
    assert keys[3] != keys[4]
    assert keys[3] == keys[5]
    assert keys[6] != keys[7]


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

    assert "adapter.proactive_prefetch" in client.calls
    assert "adapter.sync_turn" in client.calls
    assert "adapter.mutate_memory" in client.calls


def test_hermes_prefetch_queue_is_single_worker_and_cache_bounded() -> None:
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client, max_prefetch_cache_entries=3)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")

    for index in range(8):
        provider.queue_prefetch(f"query-{index}", session_id="hermes-session")
    provider.shutdown()

    assert provider.prefetch_cache_size <= 3
    assert provider.background_worker_count == 0
    prefetch_queries = [params["query"] for method, params in client.calls if method == "adapter.proactive_prefetch"]
    assert "query-7" in prefetch_queries


def test_hermes_prefetch_single_flight_deduplicates_same_hot_key() -> None:
    class BlockingPrefetchClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.call_count = 0

        def call_or_bypass(self, method: str, params: dict) -> dict:
            assert method == "adapter.proactive_prefetch"
            self.call_count += 1
            self.started.set()
            self.release.wait(timeout=2.0)
            return {
                "ok": True,
                "bypassed": False,
                "result": {"context": "single-flight context", "decision_id": "pd:single-flight"},
            }

    client = BlockingPrefetchClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("hermes-session", agent_workspace="embodied", agent_context="primary")
    provider.queue_prefetch("same hot query", session_id="hermes-session")
    assert client.started.wait(timeout=1.0)
    result: list[str] = []
    reader = threading.Thread(
        target=lambda: result.append(provider.prefetch("same hot query", session_id="hermes-session"))
    )
    reader.start()
    client.release.set()
    reader.join(timeout=2.0)
    provider.shutdown()

    assert reader.is_alive() is False
    assert result == ["single-flight context"]
    assert client.call_count == 1
    assert provider.background_worker_count == 0


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
