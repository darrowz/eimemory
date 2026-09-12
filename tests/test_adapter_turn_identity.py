from __future__ import annotations

import re

from eimemory.adapters.codex.hook import CodexHookAdapter
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService


class BridgeClient:
    def __init__(self, runtime):
        self.bridge = EIBrainRPCBridge(runtime)
        self.calls = []

    def call_or_bypass(self, method, params):
        result = dict(self.bridge.handle({"method": method, "params": params}))
        self.calls.append((method, params, result))
        return result


def test_codex_distinct_tool_calls_capture_separately_and_retries_keep_host_run(tmp_path):
    runtime = Runtime.create(root=tmp_path)

    class AttestationClient:
        def __init__(self):
            self.calls = []

        def call_or_bypass(self, method, params):
            self.calls.append(params)
            return {"ok": True, "result": {}}

    try:
        client = BridgeClient(runtime)
        attestation = AttestationClient()
        scope = {"tenant_id": "audit", "agent_id": "audit", "workspace_id": "audit", "user_id": "audit"}
        adapter = CodexHookAdapter(client=client, scope=scope, attestation_client=attestation)
        for call_id, tool, result in [
            ("call-1", "Read", "source read complete"),
            ("call-2", "Bash", "2 passed"),
            ("call-2", "Bash", "2 passed"),
        ]:
            adapter.handle("PostToolUse", {
                "session_id": "session-1", "turn_id": "turn-1", "tool_call_id": call_id,
                "tool_name": tool, "tool_input": {"command": call_id}, "tool_response": result,
            })
        captures = [result["result"] for method, _, result in client.calls if method == "adapter.sync_turn"]
        assert [result["idempotent"] for result in captures] == [False, False, True]
        assert captures[0]["record"]["record_id"] != captures[1]["record"]["record_id"]
        assert captures[1]["record"]["record_id"] == captures[2]["record"]["record_id"]
        assert [params["run_id"] for params in attestation.calls] == ["turn-1"] * 3
        assert [params["tool_call_id"] for params in attestation.calls] == ["call-1", "call-2", "call-2"]
    finally:
        runtime.close()


def test_hermes_completed_turn_gets_new_decision_while_prefetch_and_ack_retries_reuse_it(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    runtime.proactive = ProactiveRecallService(runtime, release_identity={
        "release_commit": "a" * 40, "release_version": "audit", "deployment_receipt_id": "audit-receipt",
        "release_session_id": "audit-release",
    }, control_percent=0)
    client = BridgeClient(runtime)
    provider = HermesMemoryProviderCore(client=client)
    try:
        provider.initialize("recall-session", agent_workspace="audit", user_id="audit")
        runtime.memory.ingest(
            text="Hermes project Borealis requires primary-source citations.",
            memory_type="preference", title="Borealis citation policy",
            scope=resolve_channel_scope("hermes", provider._common_params()["scope"]),
            source_id="hermes", tags=["mandatory"],
        )
        query = "What does project Borealis require?"
        for host_turn in ["host-turn-1", "host-turn-2"]:
            context = provider.prefetch(query, session_id="recall-session")
            citation = re.search(r"pm:[0-9a-f]{20}", context)
            assert citation is not None
            if host_turn == "host-turn-1":
                assert provider.prefetch(query, session_id="recall-session") == context
            provider.on_pre_llm_call(user_message=query, session_id="recall-session", turn_id=host_turn)
            if host_turn == "host-turn-1":
                provider.on_pre_llm_call(user_message=query, session_id="recall-session", turn_id=host_turn)
                assert provider.prefetch(query, session_id="recall-session") == context
            answer = "No citation used." if host_turn == "host-turn-1" else f"Required [{citation.group(0)}]."
            provider.on_post_llm_call(user_message=query, assistant_message=answer,
                                      session_id="recall-session", turn_id=host_turn)
        recalls = [result["result"] for method, _, result in client.calls if method == "adapter.proactive_prefetch"]
        assert len({result["decision_id"] for result in recalls[:3]}) == 1
        assert recalls[1]["idempotent"] is True and recalls[2]["idempotent"] is True
        assert recalls[3]["decision_id"] != recalls[0]["decision_id"]
        acks = [result["result"] for method, _, result in client.calls if method == "adapter.proactive_ack"]
        assert [result["changed"] for result in acks] == [1, 0, 1]
        terminals = [result["result"] for method, _, result in client.calls if method == "adapter.proactive_terminal"]
        assert terminals[0]["terminal_changed"] == 1
        assert terminals[1]["feedback_changed"] == 1
    finally:
        provider.shutdown()
        runtime.close()


def test_hermes_retry_after_missing_prefetch_response_keeps_attempt_identity():
    class RecoveringClient:
        def __init__(self):
            self.calls = []

        def call_or_bypass(self, method, params):
            self.calls.append((method, params))
            if len(self.calls) == 1:
                return {"ok": False, "bypassed": True, "result": None}
            return {"ok": True, "result": {"decision_id": "decision-1", "context": "recovered context"}}

    client = RecoveringClient()
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("session-a")
    assert provider.prefetch("same question") == ""
    provider.on_pre_llm_call(user_message="same question", turn_id="host-turn")
    assert len(client.calls) == 1  # No acknowledgement of an undelivered result.
    assert provider.prefetch("same question") == "recovered context"
    assert client.calls[0][1]["turn_id"] == client.calls[1][1]["turn_id"]
