"""Native capture lineage remains authoritative at every consumption boundary."""
from dataclasses import asdict
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.knowledge.turn_context import capture_turn_binding
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = dict(tenant_id="review4", agent_id="withdrawal", workspace_id="local", user_id="owner")
SESSION = "review4-session"
TURN = "review4-turn"
COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
FINAL = "部署已完成：生产为 `3.4.5 / abcdef0`。\n正式业务验收仍未通过。"


@pytest.fixture(autouse=True)
def offline_native_capture_and_identity_recall(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient

    def no_model(*_args, **_kwargs):
        raise AssertionError("external model execution forbidden in this lifecycle test")

    monkeypatch.setattr(CommandLLMClient, "complete", no_model)
    monkeypatch.delenv("EIMEMORY_L1_FORCE_QUEUE", raising=False)
    monkeypatch.setenv("EIMEMORY_L1_KICK_THREAD", "0")
    # Exact known-title recall is a native identity lookup. No candidate
    # source, selected record, or authority gate is replaced by the fixture.
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED", "0")
    monkeypatch.setenv("EIMEMORY_RERANKER_ENABLED", "0")


def _capture(runtime, variant):
    if variant == "l1_source_event":
        user_text, assistant_text = "合成资料袋由甲负责。", "已记录。"
        binding = None
    else:
        user_text, assistant_text = "检查结果", FINAL
        messages = [
            dict(id=10, role="user", content=user_text),
            dict(id=11, role="tool", tool_name="read_file", tool_call_id="call-a",
                 content=json.dumps({"content": "# atlas 3.4.5 部署验收\ncommit " + COMMIT})),
            dict(id=13, role="tool", tool_name="terminal", tool_call_id="call-b",
                 content=json.dumps({"output": "/opt/atlas/releases/" + COMMIT + "\n" +
                     json.dumps(dict(service="atlas-rpc", version="3.4.5", commit=COMMIT))})),
            dict(id=14, role="assistant", content=assistant_text),
        ]
        binding = capture_turn_binding(
            messages, user_text=user_text, assistant_text=assistant_text,
            session_id=SESSION, source_event_id=SESSION + ":" + TURN,
            scope=resolve_channel_scope("hermes", SCOPE), source_id="hermes",
        )
        assert binding is not None
    report = AgentRuntimeMemoryService(runtime).sync_turn(
        channel="hermes", scope=SCOPE, session_id=SESSION, turn_id=TURN,
        user_text=user_text, assistant_text=assistant_text, supporting_turn=binding,
    )
    assert report["ok"] is True, report
    parent = runtime.store.get_by_exact_ref(
        report["record"]["record_id"], scope=ScopeRef.from_dict(report["scope"]), source_id="hermes",
    )
    written = report["l1_atoms"] if variant == "l1_source_event" else report["project_context_records"]
    assert len(written) == 1, report
    child = runtime.store.get_by_exact_ref(written[0]["record_id"], scope=parent.scope, source_id=parent.source_id)
    assert parent.status == child.status == "active"
    if variant == "l1_source_event":
        contract = business_metadata(child.meta)["source_event_binding"]
        assert contract["schema"] == "l1_source_event.v1"
        assert contract["record_id"] == parent.record_id
        assert business_metadata(child.meta)["capture_origin"] == "l1_extract"
        assert child.source == "hermes.l1"
    else:
        contract = child.provenance["project_context"]
        assert contract["schema"] == "same_turn_release_context.v1"
        assert contract["parent_record_id"] == parent.record_id
        assert len(contract["supporting_record_ids"]) == 2
        assert business_metadata(child.meta)["capture_origin"] == "same_turn_context"
    assert any(link.relation == "derived_from" and link.target_id == parent.record_id for link in child.links)
    return parent, child, contract


def _readout(runtime, child):
    current = runtime.store.get_by_exact_ref(child.record_id, scope=child.scope, source_id=child.source_id)
    assert current is not None
    bundle = runtime.memory.recall(
        query=child.title, scope=asdict(child.scope), limit=8,
        task_context={"exact_scope_only": True, "include_evidence_only": True,
                      "include_operational_recall": True, "kinds": ["memory"],
                      "source_ids": [child.source_id]},
    )
    answer = runtime.filter_answer_evidence([current])
    # Match the actual source observation, independent of the calendar day
    # on which CI executes this regression.
    brief = runtime.build_daily_brief(scope=asdict(child.scope), date=child.time.occurred_at[:10])
    return {
        "child_status": current.status,
        "recall_ids": [record.record_id for record in bundle.items],
        "retrieval_status": bundle.to_compact_dict()["retrieval_status"],
        "answer_ids": [record.record_id for record in answer["records"]],
        "answer_excluded": answer["evidence_gate"]["excluded"],
        "brief_memory_ids": [record["record_id"] for record in brief["new_memories"]],
    }


def _assert_visible(readout, child):
    assert readout["child_status"] == "active"
    assert readout["retrieval_status"] == "evidence_found", readout
    assert readout["recall_ids"] == [child.record_id], readout
    assert readout["answer_ids"] == [child.record_id], readout
    assert child.record_id in readout["brief_memory_ids"], readout


def _assert_withdrawn(readout, child):
    assert child.record_id not in readout["recall_ids"], readout
    assert child.record_id not in readout["answer_ids"], readout
    assert child.record_id in {row["record_id"] for row in readout["answer_excluded"]}, readout
    assert child.record_id not in readout["brief_memory_ids"], readout


def _withdraw(runtime, record_id, child):
    current = runtime.store.get_by_exact_ref(record_id, scope=child.scope, source_id=child.source_id)
    assert current is not None
    current.status = "removed"
    runtime.store.rewrite(current)
    assert runtime.store.get_by_exact_ref(record_id, scope=child.scope, source_id=child.source_id).status == "removed"


@pytest.mark.parametrize(("variant", "removed_ref"), [
    ("l1_source_event", "parent"),
    ("same_turn_context", "parent"),
    ("same_turn_context", "support"),
])
def test_native_derivative_obeys_withdrawal_before_and_after_restart(tmp_path, variant, removed_ref):
    root = tmp_path / "runtime"
    with Runtime.create(root=root) as runtime:
        parent, child, contract = _capture(runtime, variant)
        _assert_visible(_readout(runtime, child), child)
        target_id = parent.record_id if removed_ref == "parent" else contract["supporting_record_ids"][0]
        _withdraw(runtime, target_id, child)
        same_instance = _readout(runtime, child)
    with Runtime.create(root=root) as reopened:
        assert reopened.store.get_by_exact_ref(target_id, scope=child.scope, source_id=child.source_id).status == "removed"
        after_restart = _readout(reopened, child)
    # Both consumers run before the assertion, preserving restart coverage in
    # a red baseline as well as after the production fix.
    _assert_withdrawn(same_instance, child)
    _assert_withdrawn(after_restart, child)


@pytest.mark.parametrize("variant", ["l1_source_event", "same_turn_context"])
def test_parent_moved_to_another_scope_cannot_authorize_original_child(tmp_path, variant):
    root = tmp_path / "runtime"
    with Runtime.create(root=root) as runtime:
        parent, child, _contract = _capture(runtime, variant)
        _assert_visible(_readout(runtime, child), child)
        # Use the public, atomic scope-move contract. A still-active record
        # with the same ID in another owner is not this child's parent.
        moved = RecordEnvelope.from_dict(parent.to_dict())
        moved.scope = ScopeRef(parent.scope.tenant_id, parent.scope.agent_id,
                               parent.scope.workspace_id, "another-owner")
        runtime.store.rewrite(moved, previous_scope=parent.scope)
        assert runtime.store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id) is None
        assert runtime.store.get_by_exact_ref(parent.record_id, scope=moved.scope, source_id=parent.source_id).status == "active"
        same_instance = _readout(runtime, child)
    with Runtime.create(root=root) as reopened:
        assert reopened.store.get_by_exact_ref(parent.record_id, scope=moved.scope, source_id=parent.source_id).status == "active"
        assert reopened.store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id) is None
        after_restart = _readout(reopened, child)
    _assert_withdrawn(same_instance, child)
    _assert_withdrawn(after_restart, child)


@pytest.mark.parametrize("variant", ["l1_source_event", "same_turn_context"])
def test_active_parent_body_replacement_invalidates_native_derivative(tmp_path, variant):
    root = tmp_path / "runtime"
    replacement = "User: 更正此前记录。\nAssistant: 当前内容不再包含原断言。"
    with Runtime.create(root=root) as runtime:
        parent, child, _contract = _capture(runtime, variant)
        _assert_visible(_readout(runtime, child), child)
        changed = RecordEnvelope.from_dict(parent.to_dict())
        changed.content["text"] = replacement
        changed.summary = replacement
        runtime.store.rewrite(changed)
        current = runtime.store.get_by_exact_ref(
            parent.record_id, scope=parent.scope, source_id=parent.source_id,
        )
        assert current.status == "active"
        assert current.content["text"] == replacement
        same_instance = _readout(runtime, child)
    with Runtime.create(root=root) as reopened:
        current = reopened.store.get_by_exact_ref(
            parent.record_id, scope=parent.scope, source_id=parent.source_id,
        )
        assert current.status == "active"
        assert current.content["text"] == replacement
        after_restart = _readout(reopened, child)
    _assert_withdrawn(same_instance, child)
    _assert_withdrawn(after_restart, child)


def test_final_recall_rechecks_l1_parent_withdrawn_after_selection(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    with Runtime.create(root=root) as runtime, Runtime.create(root=root) as reviewer:
        parent, child, _contract = _capture(runtime, "l1_source_event")
        _assert_visible(_readout(runtime, child), child)
        engine = runtime.memory.recall_engine
        select = engine._select_post_fusion_items
        calls = []

        def withdraw_after_selection(items, **kwargs):
            selected, report = select(items, **kwargs)
            assert child.record_id in {row.record_id for row in selected}
            _withdraw(reviewer, parent.record_id, child)
            calls.append(True)
            return selected, report

        monkeypatch.setattr(engine, "_select_post_fusion_items", withdraw_after_selection)
        bundle = runtime.memory.recall(
            query=child.title, scope=asdict(child.scope), limit=8,
            task_context={"exact_scope_only": True, "include_evidence_only": True,
                          "include_operational_recall": True, "kinds": ["memory"],
                          "source_ids": [child.source_id]},
        )
        assert calls == [True]
        assert reviewer.store.get_by_exact_ref(parent.record_id, scope=parent.scope,
                                               source_id=parent.source_id).status == "removed"
        assert bundle.items == bundle.rules == bundle.reflections == []
        assert bundle.explanation["retrieval_status"] == "unavailable"
        assert child.summary not in json.dumps(bundle.to_dict(), ensure_ascii=False)
        assert child.summary not in json.dumps(bundle.to_compact_dict(), ensure_ascii=False)
