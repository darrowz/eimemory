"""Native host handoff recovery; only model responses and transport failures are synthetic."""
from contextlib import closing
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.outbox import HermesOutbox
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef
from eimemory.retrieval.proactive import ProactiveRecallService

SCOPE = {"tenant_id": "default", "agent_id": "audit", "workspace_id": "isolated", "user_id": "owner"}
RELEASE = {"release_commit": "a" * 40, "release_version": "synthetic", "deployment_receipt_id": "synthetic", "release_session_id": "synthetic"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    monkeypatch.delenv("EIMEMORY_L1_KICK_THREAD", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    from eimemory.llm.command_client import CommandLLMClient
    monkeypatch.setattr(CommandLLMClient, "complete", lambda *a, **k: pytest.fail("External provider prohibited"))


class Client:
    def __init__(self, runtime):
        self.bridge = EIBrainRPCBridge(runtime)
        self.calls = []
        self.failure = ""
        self.fail_after_commit = False
        self.reject = False

    def call_or_bypass(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == self.failure and not self.fail_after_commit:
            if self.reject:
                return {"ok": False, "result": None, "diagnostic": {"http_status": 400}}
            raise TimeoutError("Synthetic lost request")
        result = dict(self.bridge.handle({"method": method, "params": params}))
        if method == self.failure:
            raise TimeoutError("Synthetic lost response after commit")
        return result


def provider(client, home, *, session="session", user="owner"):
    instance = HermesMemoryProviderCore(client=client)
    instance.initialize(session, hermes_home=str(home), agent_identity="audit", agent_workspace="isolated", user_id=user)
    return instance


def memory_rows(runtime):
    return runtime.store.list_records(kinds=["memory"], scope=ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE)), limit=100)


def sync(instance, text, ids=None, turn_id=""):
    messages = None if ids is None else [{"id": ids[0], "role": "user", "content": text},
                                         {"id": ids[1], "role": "assistant", "content": "Acknowledged."}]
    instance.sync_turn(text, "Acknowledged.", messages=messages, turn_id=turn_id)
    instance.shutdown()


class Model:
    text = ""
    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            body = json.loads(prompt)
            payload = [{"record_id": "new-0", "action": "update", "target_ids": body["new_memories"][0]["related_ids"],
                        "merged_content": self.text, "merged_type": "fact"}]
        else:
            payload = [{"content": self.text, "type": "fact", "priority": 90}]
        return SimpleNamespace(text=json.dumps(payload))


def test_distinct_identical_events_advance_latest_l1_and_host_retry_is_idempotent(tmp_path, monkeypatch):
    model = Model()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("eimemory.llm.hermes_adapter.resolve_l1_llm_client", lambda: model)
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        client = Client(runtime)
        host = provider(client, tmp_path / "profile")
        texts = ["Orchid hourly supervision setting is enabled.", "Orchid hourly supervision setting is cancelled.", "Orchid hourly supervision setting is enabled."]
        for index, text in enumerate(texts):
            model.text = text
            monkeypatch.setattr("eimemory.models.records.now_iso", lambda i=index: f"2026-10-13T00:00:0{i}Z")
            sync(host, text, (index * 2 + 1, index * 2 + 2))
            assert service.drain_l1_queue(limit=1) == 1
        rows = memory_rows(runtime)
        l0 = [r for r in rows if business_metadata(r.meta).get("memory_layer") == "l0"]
        active = [r for r in rows if business_metadata(r.meta).get("memory_layer") == "l1" and r.status == "active"]
        assert len(l0) == 3 and len(active) == 1 and active[0].summary == texts[-1]
        recovered = provider(Client(runtime), tmp_path / "profile")
        sync(recovered, texts[-1], (5, 6))
        assert len([r for r in memory_rows(runtime) if business_metadata(r.meta).get("memory_layer") == "l0"]) == 3
        assert service.drain_l1_queue(limit=1) == 1
        assert len(memory_rows(runtime)) == len(rows)


@pytest.mark.parametrize("identity", ["explicit", "messages", "local"])
@pytest.mark.parametrize("committed", [False, True])
def test_capture_recovers_request_or_response_loss_after_restart(tmp_path, identity, committed):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        client.failure = "adapter.sync_turn"
        client.fail_after_commit = committed
        host = provider(client, tmp_path / "profile")
        text = "Synthetic Orchid account belongs to Alice."
        sync(host, text, (1, 2) if identity == "messages" else None, "host-event-1" if identity == "explicit" else "")
        assert len(memory_rows(runtime)) == int(committed)
        assert host._outbox.status()["pending_writes"] == 1
        assert host._outbox.status()["attempt_count"] == 1
        assert host.dropped_write_count == 0
        first = next(params for method, params in client.calls if method == "adapter.sync_turn")
        recovered_client = Client(runtime)
        recovered = provider(recovered_client, tmp_path / "profile", session="new-session")
        recovered.shutdown()
        retried = next(params for method, params in recovered_client.calls if method == "adapter.sync_turn")
        assert retried == first
        assert len(memory_rows(runtime)) == 1
        assert recovered._outbox.status()["pending_writes"] == 0
        assert recovered._outbox.status()["recovered_count"] == 1
        path = recovered._outbox.path
        assert path and (path.stat().st_mode & 0o777) == 0o600
        assert text not in path.read_text()  # acknowledged payload removed


def test_missing_host_ids_do_not_permanently_merge_same_content(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        host = provider(Client(runtime), tmp_path / "profile")
        sync(host, "Synthetic repeated event needs a new observation.")
        sync(host, "Synthetic repeated event needs a new observation.")
        assert len(memory_rows(runtime)) == 2


@pytest.mark.parametrize("boundary", ["scope", "profile", "endpoint"])
def test_recovery_never_replays_into_a_different_authority_namespace(tmp_path, boundary):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        client.base_url = "http://synthetic-a/"
        client.failure = "adapter.sync_turn"
        host = provider(client, tmp_path / "profile")
        sync(host, "Synthetic private account belongs to Alice.", (1, 2))
        second = Client(runtime)
        second.base_url = "http://synthetic-b/" if boundary == "endpoint" else client.base_url
        recovered = provider(second, tmp_path / ("other-profile" if boundary == "profile" else "profile"),
                             user="other-user" if boundary == "scope" else "owner")
        recovered.shutdown()
        assert not [call for call in second.calls if call[0] == "adapter.sync_turn"]
        assert host._outbox.status()["pending_writes"] == 1


def test_permanent_capture_rejection_keeps_payload_with_terminal_status(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        client.failure, client.reject = "adapter.sync_turn", True
        host = provider(client, tmp_path / "profile")
        sync(host, "Synthetic rejected capture.", (1, 2))
        assert host._outbox.status()["rejected_count"] == 1
        assert host._outbox.status()["pending_writes"] == 0
        assert "Synthetic rejected capture." in host._outbox.path.read_text()
        recovered_client = Client(runtime)
        recovered = provider(recovered_client, tmp_path / "profile")
        recovered.shutdown()
        assert not [call for call in recovered_client.calls if call[0] == "adapter.sync_turn"]
        assert memory_rows(runtime) == []


def prepare_delivery(runtime, client, home, *, release=RELEASE):
    # This fixture tests persistent transport recovery, not the production
    # recall SLA. Leave room for native retrieval when other test workers
    # share the CPU; production's 0.8 s default remains unchanged.
    runtime.proactive = ProactiveRecallService(runtime, control_percent=0, release_identity=release,
                                               recall_timeout_seconds=10)
    host = provider(client, home)
    runtime.memory.ingest(text="Project Borealis requires primary-source citations.", title="Borealis requirements",
                          memory_type="preference", scope=resolve_channel_scope("hermes", SCOPE), source="hermes.memory", source_id="hermes")
    query = "What does project Borealis require?"
    context = host.prefetch(query)
    row = runtime.store.sqlite.conn.execute("SELECT decision_id,citation FROM proactive_decision_items").fetchone()
    assert row is not None and row["citation"] in context
    host.on_pre_llm_call(user_message=query, session_id="session", turn_id="host-one")
    return host, query, row["decision_id"], row["citation"]


@pytest.mark.parametrize("failure", ["adapter.proactive_ack", "adapter.proactive_terminal"])
@pytest.mark.parametrize("committed", [False, True])
def test_delivery_recovers_ack_then_used_terminal_after_restart(tmp_path, failure, committed):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        client.failure, client.fail_after_commit = failure, committed
        host.on_post_llm_call(user_message=query, assistant_message=f"I used [{citation}].", session_id="session",
                              turn_id="host-one", injected_citations=[citation])
        before = runtime.store.load_proactive_decision(decision)
        if failure == "adapter.proactive_ack":
            assert before["terminal"] is False
            assert before["items"][0]["ever_injected"] == int(committed)
            assert not [c for c in client.calls if c[0] == "adapter.proactive_terminal"]
        assert host._outbox.status()["pending_deliveries"] == 1
        recovered_client = Client(runtime)
        recovered = provider(recovered_client, tmp_path / "profile", session="later-session")
        after = runtime.store.load_proactive_decision(decision)
        assert after["terminal"] is True and after["items"][0]["ever_injected"] == 1
        assert after["items"][0]["state"] == "used"
        assert recovered._outbox.status()["pending_deliveries"] == 0
        calls = [m for m, _ in recovered_client.calls if m in {"adapter.proactive_ack", "adapter.proactive_terminal"}]
        assert calls == (["adapter.proactive_ack", "adapter.proactive_terminal"] if failure == "adapter.proactive_ack" else ["adapter.proactive_terminal"])
        assert all(p["used_citations"] == [citation] for m, p in recovered_client.calls if m == "adapter.proactive_terminal")


def test_absent_delivery_evidence_cannot_be_recovered_as_injected(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        host.on_post_llm_call(user_message=query, assistant_message="No memory cited.", session_id="session", turn_id="host-one")
        state = runtime.store.load_proactive_decision(decision)
        assert state["terminal"] is True and state["items"][0]["ever_injected"] == 0
        assert state["items"][0]["state"] == "not_used"
        assert host._outbox.status()["pending_deliveries"] == 0
        assert not [call for call in client.calls if call[0] == "adapter.proactive_ack"]


def test_failed_used_feedback_does_not_terminalize_and_recovers_stage_one(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        original = runtime.store.sqlite.upsert
        def fail_used(record, **kwargs):
            if record.kind == "feedback" and record.content.get("proactive_state") == "used":
                raise OSError("Synthetic feedback write failure")
            return original(record, **kwargs)
        monkeypatch.setattr(runtime.store.sqlite, "upsert", fail_used)
        host.on_post_llm_call(user_message=query, assistant_message=f"Used [{citation}].",
                              session_id="session", turn_id="host-one", injected_citations=[citation])
        state = runtime.store.load_proactive_decision(decision)
        assert not state["terminal"] and state["items"][0]["state"] == "injected"
        assert state["items"][0]["ever_injected"] == 1
        assert host._outbox.entries("delivery")[0][1]["stage"] == 1
        monkeypatch.setattr(runtime.store.sqlite, "upsert", original)
        healthy = Client(runtime)
        recovered = provider(healthy, tmp_path / "profile")
        state = runtime.store.load_proactive_decision(decision)
        assert state["terminal"] and state["items"][0]["state"] == "used"
        assert recovered._outbox.status()["pending_deliveries"] == 0
        assert not [m for m, _ in healthy.calls if m == "adapter.proactive_ack"]
        assert state["outcome_verified"] is False and state["outcome_success"] is None


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("cited", [False, True])
def test_delayed_ack_after_stale_close_preserves_only_host_facts(tmp_path, committed, cited):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        client.failure, client.fail_after_commit = "adapter.proactive_ack", committed
        host.on_post_llm_call(user_message=query, assistant_message=f"Used [{citation}]." if cited else "No citation.",
                              session_id="session", turn_id="host-one", injected_citations=[citation])
        with runtime.store.locked() as sql:
            sql.execute("UPDATE proactive_decisions SET created_at=?,updated_at=? WHERE decision_id=?",
                        ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", decision))
            sql.commit()
        reconciled = runtime.proactive.reconcile_stale(channel="hermes", scope=SCOPE, source_ids=["hermes"])
        assert reconciled["closed"] == 1
        stale = runtime.store.load_proactive_decision(decision)
        assert stale["terminal"] and stale["items"][0]["state"] == "not_used"
        assert stale["items"][0]["ever_injected"] == int(committed)
        healthy = Client(runtime)
        recovered = provider(healthy, tmp_path / "profile")
        after = runtime.store.load_proactive_decision(decision)
        assert after["terminal"] and after["items"][0]["ever_injected"] == 1
        assert after["items"][0]["state"] == ("used" if cited else "not_used")
        assert recovered._outbox.status()["pending_deliveries"] == 0
        assert after["outcome_verified"] is False and after["outcome_success"] is None


def test_citation_only_evidence_matches_one_normalized_host_query(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        host.on_post_llm_call(user_message="Host normalized query: " + query,
                              assistant_message=f"Used [{citation}].", session_id="session", turn_id="host-one")
        state = runtime.store.load_proactive_decision(decision)
        assert state["terminal"] and state["items"][0]["state"] == "used"
        assert state["items"][0]["ever_injected"] == 1 and not state["outcome_verified"]


@pytest.mark.parametrize("wrong_session", [False, True])
def test_unmatched_or_ambiguous_citations_cannot_ack_pending_decisions(tmp_path, wrong_session):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, _, first, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        runtime.memory.ingest(text="Project Cedar requires approved canonical artifacts.", title="Cedar requirements",
                              memory_type="preference", scope=resolve_channel_scope("hermes", SCOPE),
                              source="hermes.memory", source_id="hermes")
        context = host.prefetch("What does project Cedar require?")
        second = next(row for row in runtime.store.sqlite.conn.execute("SELECT decision_id,citation FROM proactive_decision_items")
                      if row["decision_id"] != first)
        assert second["citation"] in context
        host.on_post_llm_call(user_message="Different normalized query",
                              assistant_message=f"[{citation}] [{second['citation']}]",
                              session_id="other-session" if wrong_session else "session", turn_id="host-other")
        assert not [method for method, _ in client.calls if method == "adapter.proactive_ack"]
        for decision in (first, second["decision_id"]):
            state = runtime.store.load_proactive_decision(decision)
            assert not state["terminal"] and not state["items"][0]["ever_injected"]
        host.shutdown()


@pytest.mark.parametrize("mismatch", ["", "session_id", "turn_id", "user_id", "source_ids", "unverified_receipt", "global_receipt"])
def test_ack_retry_requires_verified_original_release_and_exact_namespace(tmp_path, monkeypatch, mismatch):
    from dataclasses import replace
    from test_adapter_receipt_review_gaps import _seed_release
    from eimemory.governance.evidence_contract import current_release_identity
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        exact = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))
        channel_scope = resolve_channel_scope("hermes", SCOPE)
        _seed_release(runtime, scope_payload=channel_scope)
        old = current_release_identity(runtime, exact)
        client = Client(runtime)
        host, _, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile", release=None)
        _seed_release(runtime, commit="e" * 40, scope_payload=channel_scope)
        args = dict(channel="hermes", scope=SCOPE, source_ids=["hermes"], session_id="session",
                    turn_id=runtime.store.load_proactive_decision(decision)["turn_id"], decision_id=decision,
                    injected_citations=[citation])
        if mismatch in {"session_id", "turn_id"}:
            args[mismatch] = "different"
        elif mismatch == "user_id":
            args["scope"] = {**SCOPE, "user_id": "different"}
        elif mismatch == "source_ids":
            args["source_ids"] = ["other"]
        elif mismatch in {"unverified_receipt", "global_receipt"}:
            original_get = runtime.store.get_by_id
            receipt = original_get(old.receipt_id, scope=exact)
            foreign = replace(receipt, scope=replace(exact, user_id="")) if mismatch == "global_receipt" else None
            monkeypatch.setattr(runtime.store, "get_by_id", lambda rid, **kw: foreign if rid == old.receipt_id else original_get(rid, **kw))
        service = AgentRuntimeMemoryService(runtime)
        result = service.proactive_ack(**args)
        state = runtime.store.load_proactive_decision(decision)
        assert result["ok"] == (not mismatch)
        assert state["items"][0]["ever_injected"] == int(not mismatch)
        assert not state["terminal"] and not state["outcome_verified"]
        if not mismatch:
            assert service.proactive_ack(**args)["changed"] == 0
            assert state["release_identity"]["deployment_receipt_id"] == old.receipt_id


@pytest.mark.parametrize("unsafe", ["symlink", "world_readable", "corrupt", "directory"])
def test_unsafe_outbox_is_preserved_and_cannot_claim_success(tmp_path, unsafe):
    box = HermesOutbox(str(tmp_path), {"scope": SCOPE})
    box.put("one", "write", {"synthetic": "private"})
    path = box.path
    if unsafe == "symlink":
        victim = tmp_path / "victim"
        victim.write_text("do not touch")
        path.unlink()
        path.symlink_to(victim)
    elif unsafe == "world_readable":
        path.chmod(0o644)
    elif unsafe == "corrupt":
        path.write_text("{invalid")
    else:
        path.parent.chmod(0o777)
    try:
        recovered = HermesOutbox(str(tmp_path), {"scope": SCOPE})
        assert recovered.status()["error"] == "outbox_unavailable"
        with pytest.raises((OSError, ValueError)):
            recovered.put("two", "write", {"synthetic": "new"})
        if unsafe == "symlink":
            assert victim.read_text() == "do not touch"
        if unsafe == "corrupt":
            assert path.read_text() == "{invalid"
    finally:
        path.parent.chmod(0o700)


def test_outbox_capacity_never_evicts_an_older_unacknowledged_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.adapters.hermes.outbox.MAX_ENTRIES", 2)
    box = HermesOutbox(str(tmp_path), {"scope": SCOPE})
    box.put("one", "write", {"event": 1})
    box.put("two", "write", {"event": 2})
    with pytest.raises(ValueError, match="capacity"):
        box.put("three", "write", {"event": 3})
    assert {key for key, _ in box.entries()} == {"one", "two"}


def test_same_event_payload_conflict_does_not_replace_original(tmp_path):
    box = HermesOutbox(str(tmp_path), {"scope": SCOPE})
    assert box.put("one", "write", {"event": 1})
    assert box.put("one", "write", {"event": 1}) is False
    with pytest.raises(ValueError, match="conflict"):
        box.put("one", "write", {"event": 2})
    assert box.entries()[0][1]["payload"] == {"event": 1}


def test_outage_preserves_event_order_and_resumes_on_next_hook(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        client.failure = "adapter.sync_turn"
        host = provider(client, tmp_path / "profile")
        sync(host, "Synthetic first event.", (1, 2))
        sync(host, "Synthetic second event.", (3, 4))
        # Later events stay behind an unconfirmed predecessor.
        assert [p["user_text"] for m, p in client.calls if m == "adapter.sync_turn"] == ["Synthetic first event."] * 2
        assert host._outbox.status()["pending_writes"] == 2
        client.failure = ""
        host.on_pre_llm_call(user_message="next turn", session_id="session", turn_id="new-turn")
        host.shutdown()
        assert [p["user_text"] for m, p in client.calls if m == "adapter.sync_turn"][-2:] == ["Synthetic first event.", "Synthetic second event."]
        assert len(memory_rows(runtime)) == 2
        assert host._outbox.status()["pending_writes"] == 0


def test_ack_confirmation_stage_must_be_persisted_before_terminal(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        original = host._outbox.update
        def stage_failure(key, **kwargs):
            if kwargs.get("stage") == 1:
                raise OSError("Synthetic fsync failure")
            return original(key, **kwargs)
        monkeypatch.setattr(host._outbox, "update", stage_failure)
        host.on_post_llm_call(user_message=query, assistant_message=f"Used [{citation}].", session_id="session", turn_id="host-one")
        before = runtime.store.load_proactive_decision(decision)
        assert before["terminal"] is False and before["items"][0]["ever_injected"] == 1
        assert not [call for call in client.calls if call[0] == "adapter.proactive_terminal"]
        assert host._outbox.status()["pending_deliveries"] == 1
        recovered = provider(Client(runtime), tmp_path / "profile")
        after = runtime.store.load_proactive_decision(decision)
        assert after["terminal"] is True and after["items"][0]["state"] == "used"
        assert recovered._outbox.status()["pending_deliveries"] == 0


@pytest.mark.parametrize("response", [{"ok": True, "result": {}}, {"ok": True, "result": {"ok": False}},
                                      {"ok": True, "bypassed": True, "result": {"ok": True}}])
def test_ack_requires_explicit_confirmation(tmp_path, response):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        class Unconfirmed(Client):
            def call_or_bypass(self, method, params):
                if method == "adapter.proactive_ack":
                    self.calls.append((method, deepcopy(params)))
                    return response
                return super().call_or_bypass(method, params)
        client = Unconfirmed(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        host.on_post_llm_call(user_message=query, assistant_message=f"Used [{citation}].", session_id="session", turn_id="host-one")
        state = runtime.store.load_proactive_decision(decision)
        assert state["terminal"] is False and state["items"][0]["ever_injected"] == 0
        assert host._outbox.status()["pending_deliveries"] == 1
        assert not [call for call in client.calls if call[0] == "adapter.proactive_terminal"]


def test_permanent_ack_rejection_never_authorizes_terminal(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host, query, decision, citation = prepare_delivery(runtime, client, tmp_path / "profile")
        client.failure, client.reject = "adapter.proactive_ack", True
        host.on_post_llm_call(user_message=query, assistant_message=f"Used [{citation}].", session_id="session", turn_id="host-one")
        assert host._outbox.status()["pending_deliveries"] == 0
        assert host._outbox.status()["rejected_count"] == 1
        assert not runtime.store.load_proactive_decision(decision)["terminal"]
        healthy = Client(runtime)
        provider(healthy, tmp_path / "profile")
        assert not [call for call in healthy.calls if call[0] in {"adapter.proactive_ack", "adapter.proactive_terminal"}]


def test_interleaved_outbox_owners_preserve_entries_and_exclude_consumers(tmp_path):
    first = HermesOutbox(str(tmp_path), {"scope": SCOPE})
    second = HermesOutbox(str(tmp_path), {"scope": SCOPE})
    first.put("one", "write", {"event": 1})
    second.put("two", "write", {"event": 2})
    with first.consumer() as acquired:
        assert acquired
        with second.consumer() as other:
            assert other is False
        # Enqueue has a separate short lock even while a consumer owns RPC work.
        second.put("three", "write", {"event": 3})
    first.update("one", outcome="completed")
    assert [key for key, _ in second.entries()] == ["two", "three"]


def test_missing_profile_refuses_unrecoverable_capture_instead_of_sending(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host = HermesMemoryProviderCore(client=client)
        host.initialize("session", agent_identity="audit", agent_workspace="isolated", user_id="owner")
        sync(host, "Synthetic capture requires a durable profile.", (1, 2))
        assert not [call for call in client.calls if call[0] == "adapter.sync_turn"]
        assert memory_rows(runtime) == []
        status = host._outbox.status()
        assert status["error"] == "outbox_path_unconfigured" and status["durable"] is False
        assert status["pending_writes"] is None
        assert host.dropped_write_count == 1
        assert host._handoff_error == "capture_handoff_not_retained"


@pytest.mark.parametrize("level", ["profile", "ancestor"])
def test_writable_profile_or_unprotected_ancestor_cannot_replace_private_state(tmp_path, level):
    ancestor = tmp_path / "ancestor"
    profile = ancestor / "profile"
    profile.mkdir(parents=True, mode=0o700)
    unsafe = profile if level == "profile" else ancestor
    unsafe.chmod(0o777)
    try:
        box = HermesOutbox(str(profile), {"scope": SCOPE})
        with pytest.raises(ValueError, match="ancestor"):
            box.put("one", "write", {"event": 1})
        assert box.status()["error"] == "outbox_unavailable"
        assert not box.path.exists()
    finally:
        unsafe.chmod(0o700)


def test_sticky_shared_ancestor_preserves_owned_private_profile(tmp_path):
    shared = tmp_path / "sticky"
    shared.mkdir(mode=0o777)
    shared.chmod(0o1777)
    box = HermesOutbox(str(shared / "profile"), {"scope": SCOPE})
    assert box.put("one", "write", {"event": 1})
    assert box.status()["durable"] is True and box.status()["error"] == ""


def test_capture_commit_checkpoint_failure_waits_for_hook_without_hot_successors(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        client = Client(runtime)
        host = provider(client, tmp_path / "profile")
        starts = []
        with monkeypatch.context() as blocked:
            blocked.setattr(host, "_start_write_worker", lambda: starts.append(True))
            sync(host, "Synthetic first event.", (1, 2))
            sync(host, "Synthetic second event.", (3, 4))
            starts.clear()
            def failed_checkpoint(*args, **kwargs):
                raise OSError("Synthetic fsync failure after RPC commit")
            blocked.setattr(host._outbox, "update", failed_checkpoint)
            host._write_worker(host._outbox, client)
            assert len([c for c in client.calls if c[0] == "adapter.sync_turn"]) == 1
            assert starts == []
            assert host._outbox.status()["pending_writes"] == 2
            assert len(memory_rows(runtime)) == 1
        host.on_pre_llm_call(user_message="next turn", session_id="session", turn_id="new-turn")
        host.shutdown()
        assert host._outbox.status()["pending_writes"] == 0
        assert len(memory_rows(runtime)) == 2
