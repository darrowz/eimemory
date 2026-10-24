"""A new Hermes write can restore removed content without rewriting history."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.models.records import LinkRef, ScopeRef


SCOPE = {"tenant_id": "audit", "agent_id": "fixture", "workspace_id": "isolated", "user_id": "owner"}
TEXT = "Synthetic project Orchid requires independent source verification."


def add(service, key, **changes):
    return service.mutate_memory(**{
        "channel": "hermes", "scope": SCOPE, "source_id": "hermes", "target": "memory",
        "action": "add", "content": TEXT, "idempotency_key": key,
        "provenance": {"write_origin": "hermes.memory_write", "session_id": "session"}, **changes})


def remove(service, result, key):
    return add(service, key, action="remove", content="", target_record_id=result["record"]["record_id"],
               expected_revision=result["content_revision"])


def get(runtime, result):
    row = result["record"]
    return runtime.store.get_by_exact_ref(row["record_id"], scope=ScopeRef.from_dict(row["scope"]), source_id=row["source_id"])


def rewrite(runtime, record):
    def update(sqlite):
        sqlite.upsert(record, commit=False)
        return None, [record], []
    runtime.store.mutate_records_atomically(update)


def active(runtime, scope):
    return runtime.store.list_records(kinds=["memory"], scope=scope, status="active", source_ids=["hermes"])


def test_new_event_restores_removed_content_and_repeated_cycles_keep_history(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        first = previous = add(service, "add-0")
        assert first["ok"]
        retired = []
        for cycle in range(1, 4):
            deletion = remove(service, previous, f"remove-{cycle}")
            assert deletion["ok"]
            retired.extend([get(runtime, previous).to_dict(), get(runtime, deletion).to_dict()])
            restored = add(service, f"add-{cycle}")
            assert restored["ok"] is True and restored["idempotent"] is False
            assert restored["record"]["record_id"] not in {row["record_id"] for row in retired}
            assert len(active(runtime, restored["scope"])) == 1
            assert add(service, f"add-{cycle}")["record"] == restored["record"]
            for old in retired:
                assert runtime.store.get_by_exact_ref(old["record_id"], scope=ScopeRef.from_dict(old["scope"]),
                                                     source_id=old["source_id"]).to_dict() == old
            previous = restored
        old_retry = add(service, "add-0")
        assert old_retry["ok"] is True and old_retry["idempotent"] is True
        assert old_retry["record"]["record_id"] == first["record"]["record_id"]
        assert old_retry["record"]["status"] == "removed"
        assert len(active(runtime, previous["scope"])) == 1


def test_different_new_event_cannot_duplicate_an_active_restoration(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        assert remove(service, initial, "remove-initial")["ok"]
        restored = add(service, "add-restored")
        assert restored["ok"]
        duplicate = add(service, "another-add-event")
        assert duplicate == {"ok": False, "error": "mutation_target_conflict"}
        assert len(active(runtime, restored["scope"])) == 1


def test_removed_replacement_can_restore_but_superseded_content_cannot(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        replacement_text = "Synthetic project Orchid now requires two independent source reviews."
        replacement = add(service, "replace-event", action="replace", content=replacement_text,
                          target_record_id=initial["record"]["record_id"], expected_revision=initial["content_revision"])
        assert replacement["ok"]
        assert remove(service, replacement, "remove-replacement")["ok"]
        restored = add(service, "restore-replacement", content=replacement_text)
        assert restored["ok"] and restored["record"]["record_id"] != replacement["record"]["record_id"]
        assert add(service, "restore-obsolete-content") == {"ok": False, "error": "mutation_target_conflict"}
        assert get(runtime, initial).status == "superseded"
        assert len(active(runtime, restored["scope"])) == 1


@pytest.mark.parametrize("status", ["quarantined", "superseded", "archived"])
def test_old_removed_history_cannot_bypass_a_later_inactive_incarnation(tmp_path, status):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        remove(service, initial, "remove-initial")
        restored = add(service, "add-restored")
        assert restored["ok"]
        current = get(runtime, restored)
        current.status = status
        current.meta["mutation_state"] = status
        rewrite(runtime, current)
        denied = add(service, "try-another-event")
        assert denied["ok"] is False and denied["error"] == "mutation_target_conflict"
        assert get(runtime, restored).status == status
        assert active(runtime, restored["scope"]) == []


@pytest.mark.parametrize("fault", ["status", "kind", "producer", "action", "predecessor", "forward_link", "back_link", "non_recallable",
                                  "content", "request_digest", "provenance"])
def test_restoration_requires_native_removal_evidence(tmp_path, fault):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        deletion = remove(service, initial, "remove-initial")
        tombstone = get(runtime, deletion)
        if fault == "back_link":
            original = get(runtime, initial)
            original.links = []
            rewrite(runtime, original)
        else:
            if fault == "status":
                tombstone.status = "quarantined"
            elif fault == "kind":
                tombstone.kind = "replay_result"
            elif fault == "producer":
                tombstone.source = "untrusted.fixture"
            elif fault == "action":
                tombstone.meta["mutation_action"] = "add"
            elif fault == "predecessor":
                tombstone.meta["predecessor_record_id"] = "mem_another_parent"
            elif fault == "forward_link":
                tombstone.links = [LinkRef(relation="removes", target_kind="record", target_id="mem_another_parent")]
            elif fault == "non_recallable":
                tombstone.meta["non_recallable"] = False
            elif fault == "content":
                tombstone.content = {"text": "Mutated removal evidence"}
            elif fault == "request_digest":
                tombstone.meta["mutation_request_digest"] = "0" * 64
            elif fault == "provenance":
                tombstone.provenance["session_id"] = "a-different-execution"
            rewrite(runtime, tombstone)
        denied = add(service, "restore-new-event")
        assert denied == {"ok": False, "error": "mutation_removal_evidence_invalid"}
        assert active(runtime, initial["scope"]) == []


@pytest.mark.parametrize("fault", ["scope", "source_id", "missing"])
def test_tombstone_readback_must_have_exact_identity(tmp_path, monkeypatch, fault):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        deletion = remove(service, initial, "remove-initial")
        tombstone_id = deletion["record"]["record_id"]
        reader = runtime.store.sqlite.get_by_exact_ref

        def read(record_id, **kwargs):
            record = reader(record_id, **kwargs)
            if record_id == tombstone_id:
                if fault == "missing":
                    return None
                if fault == "scope":
                    record.scope.user_id = "another-owner"
                else:
                    record.source_id = "another-source"
            return record

        monkeypatch.setattr(runtime.store.sqlite, "get_by_exact_ref", read)
        denied = add(service, "restore-new-event")
        assert denied == {"ok": False, "error": "mutation_removal_evidence_invalid"}


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_restore_has_one_active_incarnation(tmp_path, same_key):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        remove(service, initial, "remove-initial")
        barrier = Barrier(2)

        # Independent RuntimeStore locks/connections exercise SQLite's actual
        # transaction owner, rather than only a shared Python object lock.
        with Runtime.create(root=tmp_path) as competing_runtime:
            services = [service, AgentRuntimeMemoryService(competing_runtime)]

            def restore(index):
                barrier.wait(timeout=5)
                return add(services[index], "same-new-event" if same_key else f"new-event-{index}")

            with ThreadPoolExecutor(max_workers=2) as executor:
                reports = list(executor.map(restore, range(2)))
        assert sum(report["ok"] for report in reports) == (2 if same_key else 1)
        assert len(active(runtime, initial["scope"])) == 1
        if same_key:
            assert len({report["record"]["record_id"] for report in reports}) == 1
            assert sum(report["idempotent"] for report in reports) == 1


def test_other_owner_source_and_target_do_not_block_a_valid_restore(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        remove(service, initial, "remove-initial")
        assert add(service, "foreign-user", scope={**SCOPE, "user_id": "another-owner"})["ok"]
        assert add(service, "foreign-source", source_id="another-source")["ok"]
        assert add(service, "other-target", target="user")["ok"]
        restored = add(service, "restore-new-event")
        assert restored["ok"] is True
        matching = [record for record in active(runtime, initial["scope"]) if record.meta.get("hermes_target") == "memory"]
        assert len(matching) == 1


@pytest.mark.parametrize("target, other_target", [("memory", "user"), ("user", "memory")])
def test_other_target_history_does_not_exhaust_restore_window(tmp_path, target, other_target):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)

        def retire(target, key):
            record = add(service, f"{key}-add", target=target)
            assert record["ok"], record
            deletion = add(service, f"{key}-remove", target=target, action="remove", content="",
                           target_record_id=record["record"]["record_id"], expected_revision=record["content_revision"])
            assert deletion["ok"], deletion
            return record

        initial = retire(target, "requested")
        for index in range(32):
            retire(other_target, f"unrelated-{index}")

        restored = add(service, "restore-requested-target", target=target)
        assert restored["ok"] is True
        assert restored["record"]["record_id"] != initial["record"]["record_id"]
        assert get(runtime, initial).status == "removed"
        assert add(service, "duplicate-requested-target", target=target) == {
            "ok": False, "error": "mutation_target_conflict"}
        assert len(active(runtime, restored["scope"])) == 1


def test_recovery_history_bound_still_refuses_incomplete_same_target_history(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        # A complete 32-row history permits one fresh incarnation. Once that
        # incarnation is removed too, the next lookahead must fail closed.
        for index in range(33):
            record = add(service, f"bounded-add-{index}")
            assert record["ok"], (index, record)
            assert remove(service, record, f"bounded-remove-{index}")["ok"]
        assert add(service, "beyond-recovery-window") == {
            "ok": False, "error": "mutation_history_unavailable"}
        assert active(runtime, record["scope"]) == []


@pytest.mark.parametrize("failure", ["unsupported", "truncated"])
def test_unknown_history_cannot_certify_a_new_add(tmp_path, monkeypatch, failure):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        initial = add(service, "add-initial")
        remove(service, initial, "remove-initial")
        original = get(runtime, initial)
        calls = []

        def history(**kwargs):
            calls.append(kwargs)
            return None if failure == "unsupported" else [original] * kwargs["limit"]

        monkeypatch.setattr(runtime.store.sqlite, "list_records_by_meta_value", history)
        denied = add(service, "restore-new-event")
        assert denied == {"ok": False, "error": "mutation_history_unavailable"}
        assert calls[0]["exact_scope"] is True and calls[0]["source_ids"] == ["hermes"]
        assert active(runtime, initial["scope"]) == []


def test_native_provider_distinct_events_restore_after_remove(tmp_path, trusted_dataset_path_ancestors):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        class Client:
            def __init__(self):
                self.bridge = EIBrainRPCBridge(runtime)
                self.mutations = []

            def call_or_bypass(self, method, params):
                report = dict(self.bridge.handle({"method": method, "params": deepcopy(params)}))
                if method == "adapter.mutate_memory":
                    self.mutations.append(report)
                return report

        client = Client()
        for index, (action, content, extra) in enumerate([
            ("add", TEXT, {}), ("remove", "", {"old_text": TEXT}), ("add", TEXT, {})]):
            provider = HermesMemoryProviderCore(client=client)
            provider.initialize("session", hermes_home=str(tmp_path / "profile"), agent_identity="fixture",
                                agent_workspace="isolated", user_id="owner")
            provider.on_memory_write(action, "memory", content, {"event_id": f"native-event-{index}", **extra})
            provider.shutdown()
        assert len(client.mutations) == 3 and all(report["ok"] for report in client.mutations)
        first, _, restored = [report["result"] for report in client.mutations]
        assert restored["record"]["record_id"] != first["record"]["record_id"]
        assert get(runtime, first).status == "removed"
        assert len(active(runtime, restored["scope"])) == 1
