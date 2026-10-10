import json

import pytest

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.channel import resolve_channel_scope
from test_hermes_adapter import FakeClient

BASE = {"tenant_id": "default", "agent_id": "colleague", "workspace_id": "office", "user_id": "feishu-user"}


@pytest.fixture(autouse=True)
def clear_identity_env(monkeypatch):
    for key in ("EIMEMORY_TENANT_ID", "EIMEMORY_AGENT_ID", "EIMEMORY_DEPLOY_SCOPE_AGENT",
                "EIMEMORY_WORKSPACE_ID", "EIMEMORY_DEPLOY_SCOPE_WORKSPACE", "EIMEMORY_USER_ID", "EIMEMORY_SOURCE_IDS"):
        monkeypatch.delenv(key, raising=False)


def initialize(provider, **kwargs):
    provider.initialize("probe", agent_identity=BASE["agent_id"], agent_workspace=BASE["workspace_id"],
                        user_id=BASE["user_id"], expected_scope=BASE, expected_source_ids=["hermes"], **kwargs)


def test_wrong_gateway_scope_fails_before_rpc(monkeypatch):
    monkeypatch.setenv("EIMEMORY_AGENT_ID", "other-gateway")
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    with pytest.raises(ValueError, match="hermes_expected_scope_mismatch"):
        initialize(provider)
    assert client.calls == []


def test_wrong_sources_fail_before_rpc(monkeypatch):
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "other-team")
    client = FakeClient()
    with pytest.raises(ValueError, match="hermes_expected_sources_mismatch"):
        initialize(HermesMemoryProviderCore(client=client))
    assert client.calls == []


def test_status_exposes_actual_identity_and_import_path(monkeypatch):
    monkeypatch.setenv("EIMEMORY_AGENT_ID", "deployment-agent")
    provider = HermesMemoryProviderCore(client=FakeClient())
    provider.initialize("probe", agent_identity="colleague", agent_workspace="office", user_id="feishu-user")
    status = json.loads(provider.handle_tool_call("eimemory_status", {}))["adapter_local"]["identity"]
    assert status["effective_scope"]["agent_id"] == "deployment-agent"
    assert status["effective_scope"]["user_id"] == "feishu-user"
    assert status["context_overrides"]["agent_id"] == {"requested": "colleague", "effective": "deployment-agent"}
    assert status["source_ids"] == ["hermes"]
    assert status["process_import_path"].endswith("eimemory/adapters/hermes/provider_core.py")


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_recall_response_must_match_checked_channel_and_scope(wrong_identity):
    class Client(FakeClient):
        def call_or_bypass(self, method, params):
            self.calls.append((method, params))
            return {"ok": True, "result": {"ok": True, "channel": "hermes",
                "scope": resolve_channel_scope("hermes", {**BASE, "user_id": "other"} if wrong_identity else BASE),
                "bundle": {"items": []}}}
    client = Client()
    provider = HermesMemoryProviderCore(client=client)
    initialize(provider)
    result = json.loads(provider.handle_tool_call("eimemory_recall", {"query": "language preference"}))
    assert client.calls[-1][1]["scope"] == BASE
    assert client.calls[-1][1]["task_context"]["source_ids"] == ["hermes"]
    if wrong_identity:
        assert result == {"ok": False, "error": "hermes_response_scope_mismatch"}
    else:
        assert result["ok"] is True
        assert result["result"]["bundle"]["items"] == []  # Healthy identity does not prove recall quality.


def test_source_drift_is_rejected_before_recall(monkeypatch):
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    initialize(provider)
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "other-team")
    result = json.loads(provider.handle_tool_call("eimemory_recall", {"query": "preference"}))
    assert result == {"ok": False, "error": "hermes_expected_sources_mismatch"}
    assert not any(method == "adapter.prefetch" for method, _ in client.calls)


@pytest.mark.parametrize("item_count", [0, 1])
def test_operator_probe_returns_counts_and_never_certifies_live_session(item_count):
    from deploy.check_hermes_recall_identity import probe_hermes_recall_identity
    class Client(FakeClient):
        def call_or_bypass(self, method, params):
            self.calls.append((method, params))
            return {"ok": True, "result": {"ok": True, "channel": "hermes",
                "scope": resolve_channel_scope("hermes", BASE), "context": "PRIVATE-RECALLED-TEXT",
                "bundle": {"items": [{"summary": "PRIVATE-RECALLED-TEXT"}] * item_count}}}
    client = Client()
    provider = HermesMemoryProviderCore(client=client)
    report = probe_hermes_recall_identity(provider, scope=BASE, source_ids=["hermes"], queries=["PRIVATE-QUERY"])
    assert report["ok"] is True
    assert report["recall_has_evidence"] == bool(item_count)
    assert report["checks"][0]["item_count"] == item_count
    assert not report["live_session_verified"] and not report["certifies_recall_quality"]
    assert "PRIVATE" not in json.dumps(report)
    assert all(method == "adapter.prefetch" for method, _ in client.calls)


def test_explicit_recall_honors_sources_through_real_rpc_and_retrieval(tmp_path):
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", BASE))
    records = [runtime.store.append(RecordEnvelope.create(
        kind="memory", source_id=source, title="regional reporting preference",
        summary="Use regional reporting preference for my weekly brief.",
        content={"text": "Use regional reporting preference for my weekly brief."},
        scope=scope, meta={"memory_type": "preference"})) for source in ("hermes", "default")]
    bridge = EIBrainRPCBridge(runtime)
    class Client:
        def call_or_bypass(self, method, params):
            return bridge.handle({"method": method, "params": params})
    provider = HermesMemoryProviderCore(client=Client())
    try:
        initialize(provider)
        result = json.loads(provider.handle_tool_call("eimemory_recall", {"query": "regional reporting preference"}))
        assert result["ok"] is True
        bundle = result["result"]["bundle"]
        ids = {item["record_id"] for key in ("items", "persona", "rules", "reflections") for item in bundle[key]}
        assert records[0].record_id in ids
        assert records[1].record_id not in ids
        from deploy.check_hermes_recall_identity import probe_hermes_recall_identity
        report = probe_hermes_recall_identity(provider, scope=BASE, source_ids=["hermes"],
                                              queries=["regional reporting preference"])
        assert report["recall_has_evidence"] is True
        assert report["checks"][0]["persona_count"] == 1
        assert report["checks"][0]["item_count"] == 0
    finally:
        provider.shutdown()
        runtime.close()
