"""Empty retrievals must close feedback without inventing task success."""
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService


def test_empty_decision_closes_in_real_runtime(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    runtime.proactive = ProactiveRecallService(
        runtime, control_percent=0,
        release_identity={"release_commit": "a" * 40, "release_version": "1.13.16",
                          "deployment_receipt_id": "receipt-a", "release_session_id": "release-a"},
    )
    bridge = EIBrainRPCBridge(runtime)
    calls = []

    class Client:
        def call_or_bypass(self, method, params):
            calls.append((method, params))
            return dict(bridge.handle({"method": method, "params": params}))

    provider = HermesMemoryProviderCore(client=Client())
    provider.initialize("empty-session", agent_workspace="empty-workspace", user_id="tester")
    query = "What is the nonexistent Borealis launch date?"
    try:
        assert provider.prefetch(query, session_id="empty-session") == ""
        before = runtime.store.sqlite.conn.execute(
            "SELECT decision_id,terminal FROM proactive_decisions WHERE session_id=?",
            ("empty-session",),
        ).fetchone()
        assert before is not None and before["terminal"] == 0
        prefetch = next(p for m, p in calls if m == "adapter.proactive_prefetch")
        namespace = {k: prefetch[k] for k in ("channel", "scope", "source_ids", "session_id", "turn_id")}
        namespace["decision_id"] = before["decision_id"]
        denied = bridge.handle({"method": "adapter.proactive_terminal", "params": {
            **namespace, "session_id": "wrong-session", "used_citations": [], "terminal_outcome": {},
        }})
        assert not denied.get("ok") or not denied.get("result", {}).get("ok")
        assert runtime.store.sqlite.conn.execute(
            "SELECT terminal FROM proactive_decisions WHERE decision_id=?", (before["decision_id"],)
        ).fetchone()[0] == 0
        provider.on_pre_llm_call(user_message=query, session_id="empty-session", turn_id="host-turn")
        provider.on_post_llm_call(user_message=query, assistant_message="No supporting memory found.",
                                  session_id="empty-session", turn_id="host-turn")
        after = runtime.store.sqlite.conn.execute(
            "SELECT terminal,outcome_verified,outcome_success FROM proactive_decisions WHERE decision_id=?",
            (before["decision_id"],),
        ).fetchone()
        assert after["terminal"] == 1
        assert not [p for m, p in calls if m == "adapter.proactive_ack"]
        assert not after["outcome_verified"]
        assert not after["outcome_success"]
        terminal = [p for m, p in calls if m == "adapter.proactive_terminal"]
        assert len(terminal) == 1
        assert terminal[0]["used_citations"] == []
        assert terminal[0]["terminal_outcome"] == {}
        assert not [p for m, p in calls if m == "adapter.record_terminal"]
    finally:
        provider.shutdown()
        runtime.close()
