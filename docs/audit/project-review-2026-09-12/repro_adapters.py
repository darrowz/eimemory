"""Offline/loopback reproductions for the adapter review of e202c3d.

Run with Python from any directory. All runtime state and credentials are
temporary; no production configuration, service, or token is used.
Assertions describe the confirmed defects, so they will fail after fixes.
"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from eimemory.adapters.codex.hook import CodexHookAdapter
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.hermes.provider_registry import get_hermes_provider
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService

SCOPE = {"tenant_id": "audit", "agent_id": "audit", "workspace_id": "audit", "user_id": "audit"}


@contextmanager
def isolated_environment():
    saved = {key: value for key, value in os.environ.items()
             if key.startswith("EIMEMORY_") or key.lower().endswith("_proxy")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        yield
    finally:
        os.environ.update(saved)


class BridgeClient:
    def __init__(self, runtime):
        self.bridge = EIBrainRPCBridge(runtime)
        self.calls = []

    def call_or_bypass(self, method, params):
        result = self.bridge.handle({"method": method, "params": params})
        self.calls.append((method, params, result))
        return result


def business_rejections_open_transport_circuit(root):
    with Runtime.create(root=root) as runtime:
        token = secrets.token_hex(32)
        server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=token)
        server.start()
        try:
            url = f"http://127.0.0.1:{server.address[1]}/"
            params = {"channel": "codex", "scope": SCOPE, "text": "ok",
                      "event_id": "short-note", "memory_type": "durable_fact"}
            request = urllib.request.Request(url,
                data=json.dumps({"method": "adapter.remember", "params": params}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST")
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    status, raw = response.status, json.loads(response.read())
            except urllib.error.HTTPError as response:
                status, raw = response.code, json.loads(response.read())
            assert status == 400 and raw["result"]["record"]["status"] == "rejected"

            client = AgentRuntimeRPCClient(base_url=url, auth_token=token, timeout_seconds=2)
            rejected = [client.call_or_bypass("adapter.remember", params) for _ in range(3)]
            status_params = {"channel": "codex", "scope": SCOPE}
            blocked = client.call_or_bypass("adapter.status", status_params)
            healthy = AgentRuntimeRPCClient(base_url=url, auth_token=token,
                timeout_seconds=2).call_or_bypass("adapter.status", status_params)
            assert all(result.get("error") == "adapter_unavailable" and result.get("result") is None
                       for result in rejected)
            assert blocked["error"] == "circuit_open" and healthy["ok"] is True
            return {"business_http_status": status, "business_record_status": "rejected",
                    "client_error_after_rejection": rejected[0]["error"],
                    "same_client_status_error": blocked["error"], "fresh_client_status_ok": healthy["ok"]}
        finally:
            server.stop()


def hermes_l0_budget_does_not_reset_per_turn(root):
    spec = importlib.util.spec_from_file_location("audit_hermes_provider",
        REPO / "integrations/hermes/eimemory/__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Runtime.create(root=root) as runtime:
        client = BridgeClient(runtime)
        provider = module.EIMemoryProvider(client=client)
        provider.initialize("hermes-a", agent_workspace="audit", user_id="audit")
        try:
            provider.on_pre_llm_call(user_message="first question", session_id="hermes-a", turn_id="turn-1")
            first = [json.loads(provider.handle_tool_call("eimemory_search_l0", {"query": "Borealis"}))
                     for _ in range(3)]
            provider.on_post_llm_call(user_message="first question", assistant_message="done",
                                     session_id="hermes-a", turn_id="turn-1")
            provider.on_pre_llm_call(user_message="different question", session_id="hermes-a", turn_id="turn-2")
            second = json.loads(provider.handle_tool_call("eimemory_search_l0", {"query": "another project"}))
            assert get_hermes_provider("hermes-a") is provider
            assert all(result["ok"] for result in first)
            assert second["error"] == "l0_search_budget_exhausted"
            calls = sum(method == "adapter.search_l0" for method, _, _ in client.calls)
            assert calls == 3
            return {"same_registered_provider": True, "first_turn_successes": 3,
                    "second_turn_first_call_error": second["error"], "server_l0_call_count": calls}
        finally:
            provider.shutdown()


def codex_long_response_citation_is_recorded_as_not_used(root):
    with Runtime.create(root=root) as runtime:
        runtime.proactive = ProactiveRecallService(runtime, release_identity={
            "release_commit": "a" * 40, "release_version": "audit",
            "deployment_receipt_id": "audit-receipt", "release_session_id": "audit-release",
        }, control_percent=0)
        runtime.memory.ingest(text="Project Borealis requires primary-source citations.",
            memory_type="preference", title="Borealis requirements",
            scope=resolve_channel_scope("codex", SCOPE), source="codex.memory", source_id="codex")
        client = BridgeClient(runtime)
        adapter = CodexHookAdapter(client=client, scope=SCOPE)
        prompt = {"session_id": "codex-a", "turn_id": "turn-a",
                  "prompt": "What does project Borealis require?"}
        injected = adapter.handle("UserPromptSubmit", prompt)
        citation = re.search(r"pm:[0-9a-f]{20}",
            injected["hookSpecificOutput"]["additionalContext"]).group(0)
        response = "Explanation. " * 180 + f"Borealis requires citations [{citation}]."
        assert response.index(citation) > 2000
        adapter.handle("Stop", {**prompt, "last_assistant_message": response})
        decision_result = next(result["result"] for method, _, result in client.calls
                               if method == "adapter.proactive_prefetch")
        submitted = next(params for method, params, _ in client.calls
                         if method == "adapter.proactive_terminal")
        decision = runtime.store.load_proactive_decision(decision_result["decision_id"])
        states = [item["state"] for item in decision["items"]]
        assert submitted["used_citations"] == [] and states == ["not_used"]
        return {"citation_offset": response.index(citation), "submitted_used_citations": [],
                "persisted_feedback_states": states}


if __name__ == "__main__":
    with isolated_environment(), TemporaryDirectory(prefix="eimemory-adapters-review-") as temporary:
        root = Path(temporary)
        print(json.dumps({
            "http_business_rejection": business_rejections_open_transport_circuit(root / "http"),
            "hermes_per_turn_budget": hermes_l0_budget_does_not_reset_per_turn(root / "hermes"),
            "codex_citation_feedback": codex_long_response_citation_is_recorded_as_not_used(root / "codex"),
        }, ensure_ascii=True, indent=2))
