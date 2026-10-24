from __future__ import annotations

from contextlib import closing

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


SCOPE = {"tenant_id": "audit", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "alice"}
TEXT = "Release acceptance requires independently verified deployment evidence."


@pytest.mark.parametrize("channel", ["codex", "hermes", "openclaw"])
@pytest.mark.parametrize("change", [
    {"text": "Release acceptance requires a different source and a new review."},
    {"title": "A different memory"},
    {"memory_type": "preference"},
    {"meta": {"semantic_key": "a-different-fact"}},
    {"force_capture": False},
])
def test_remember_reused_event_cannot_acknowledge_a_different_request(tmp_path, channel, change):
    params = dict(channel=channel, scope=SCOPE, event_id="one-host-event", text=TEXT,
                  force_capture=True, create_probe=False)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.remember(**params)
        assert first["ok"] is True
        before = runtime.store.get_by_exact_ref(
            first["record"]["record_id"], scope=ScopeRef.from_dict(resolve_channel_scope(channel, SCOPE)),
            source_id=channel,
        ).to_dict()
        with pytest.raises(ValueError, match="conflict"):
            service.remember(**{**params, **change})
        after = runtime.store.get_by_exact_ref(
            before["record_id"], scope=ScopeRef.from_dict(before["scope"]), source_id=channel,
        )
        assert after.to_dict() == before


def test_remember_exact_retry_survives_restart_without_running_create_probe(tmp_path, monkeypatch):
    params = dict(channel="hermes", scope=SCOPE, event_id="retry-event", text=TEXT,
                  force_capture=True, meta={"semantic_key": "release-evidence"})
    with closing(Runtime.create(root=tmp_path)) as runtime:
        first = AgentRuntimeMemoryService(runtime).remember(**params)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        def unexpected_probe(*args, **kwargs):
            raise AssertionError("a committed retry must not depend on a new recall probe")
        monkeypatch.setattr("eimemory.adapters.runtime.service.gate_host_create", unexpected_probe)
        second = AgentRuntimeMemoryService(runtime).remember(**params)
        assert second["ok"] is True and second["idempotent"] is True
        assert second["record"] == first["record"]


def test_remember_retry_does_not_reactivate_a_withdrawn_record(tmp_path):
    params = dict(channel="codex", scope=SCOPE, event_id="withdrawn-event", text=TEXT,
                  force_capture=True, create_probe=False)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.remember(**params)
        record = runtime.store.get_by_exact_ref(
            first["record"]["record_id"], scope=ScopeRef.from_dict(first["scope"]), source_id="codex")
        record.status = "quarantined"
        def withdraw(sqlite):
            sqlite.upsert(record, commit=False)
            return None, [record], []
        runtime.store.mutate_records_atomically(withdraw)
        retry = service.remember(**params)
        assert retry["ok"] is False and retry["idempotent"] is True
        assert retry["record"]["status"] == "quarantined"


def test_sync_turn_conflicting_retry_does_not_enqueue_new_source_text(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    params = dict(channel="hermes", scope=SCOPE, session_id="session", turn_id="turn",
                  user_text=TEXT, assistant_text="The release acceptance rule is recorded.")
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.sync_turn(**params)
        assert first["l1_queued"] is True
        queue_path = runtime.store.root / "state" / "l1_extract_queue.json"
        original_queue = queue_path.read_bytes()
        with pytest.raises(ValueError, match="conflict"):
            service.sync_turn(**{**params, "user_text": "The previous release rule is now cancelled."})
        assert queue_path.read_bytes() == original_queue


def test_rpc_remember_conflict_is_a_failed_business_response(tmp_path):
    params = dict(channel="codex", scope=SCOPE, event_id="rpc-event", text=TEXT, force_capture=True)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        first = bridge.handle({"method": "adapter.remember", "params": params})
        second = bridge.handle({"method": "adapter.remember", "params": {
            **params, "text": "This request uses the same host event but contradicts its content."}})
        assert first["ok"] is True
        assert second["ok"] is False


def test_distinct_remember_events_keep_existing_fact_replacement_contract(tmp_path):
    params = dict(channel="codex", scope=SCOPE, title="Release approval policy",
                  force_capture=True, create_probe=False)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.remember(**params, event_id="old", text=TEXT)
        second = service.remember(**params, event_id="new", text="Release approval now requires two independent reviews.")
        assert first["ok"] and second["ok"]
        old = runtime.store.get_by_exact_ref(
            first["record"]["record_id"], scope=ScopeRef.from_dict(first["scope"]), source_id="codex")
        assert old.status == "superseded"


@pytest.mark.parametrize("channel", ["openclaw", "hermes", "codex"])
def test_remember_persists_the_exact_advertised_owner_scope(tmp_path, channel):
    scope = {"tenant_id": "isolated", "agent_id": "unrelated-agent",
             "workspace_id": "unrelated-workspace", "user_id": "other-user"}
    params = dict(channel=channel, scope=scope, event_id="exact-owner", text=TEXT,
                  force_capture=True, create_probe=False)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.remember(**params)
        expected = resolve_channel_scope(channel, scope)
        assert first["ok"] is True
        assert first["record"]["scope"] == expected == first["scope"]
        second = service.remember(**params)
        assert second["idempotent"] is True
        assert second["record"]["record_id"] == first["record"]["record_id"]
        stored = runtime.store.get_by_exact_ref(
            first["record"]["record_id"], scope=ScopeRef.from_dict(expected), source_id=channel)
        assert stored is not None and stored.scope == ScopeRef.from_dict(expected)


@pytest.mark.parametrize("field", ["semantic_key", "memory_layer"])
@pytest.mark.parametrize("nested", [False, True])
def test_legacy_remember_retry_checks_explicit_business_identity(tmp_path, field, nested):
    def metadata(value):
        return {"business_meta": {field: value}} if nested else {field: value}
    original = "release-rule" if field == "semantic_key" else "l0"
    changed = "different-rule" if field == "semantic_key" else "l1"
    params = dict(channel="hermes", scope=SCOPE, event_id="legacy-event", text=TEXT,
                  title="Legacy explicit memory", force_capture=True, create_probe=False,
                  meta=metadata(original))
    with closing(Runtime.create(root=tmp_path)) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = service.remember(**params)
        record = runtime.store.get_by_exact_ref(
            first["record"]["record_id"], scope=ScopeRef.from_dict(first["scope"]), source_id="hermes")
        record.meta.pop("ingest_request_digest", None)
        record.meta.get("business_meta", {}).pop("ingest_request_digest", None)
        def write_legacy(sqlite):
            sqlite.upsert(record, commit=False)
            return None, [record], []
        runtime.store.mutate_records_atomically(write_legacy)
        assert service.remember(**params)["idempotent"] is True
        with pytest.raises(ValueError, match="conflict"):
            service.remember(**{**params, "meta": metadata(changed)})
