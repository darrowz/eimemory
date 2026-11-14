"""Run only in the parent's containment; source echo is not a model-quality test."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import pytest

from test_ordinary_chat_temporal_semantics import SCOPE, assert_evidence, chat
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.knowledge.chat_temporal import KEY, ground_assertion, resolve_candidates
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef
from eimemory.retrieval.answer_requirements import requested_attribute, supports_answer_requirements
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.relevance import RelevanceAdmission, RelevanceConfig


def scoped():
    return resolve_channel_scope("hermes", SCOPE)


def request_for(query):
    return CandidateRequest.create(query=query, scope=scoped(), source_ids=["hermes"])


def children(chat):
    return [r for r in chat.runtime.store.list_records(
        kinds=["memory"], scope=ScopeRef.from_dict(scoped()), source_ids=["hermes"], limit=100)
        if business_metadata(r.meta).get("memory_layer") == "l1"]


def seed(chat):
    old = chat.say("自2021-01-01起，青岚项目预算是10万元。")
    new = chat.say("自2021-03-01起，青岚项目预算改为12万元。")
    return old, new


@pytest.mark.parametrize("entity,attribute,old_value,new_value,question", [
    ("青岚项目", "预算", "10万元", "12万元", "多少"),
    ("沧澜订单", "定金", "3万元", "4万元", "多少"),
    ("星棠设备", "归属地", "北库", "南库", "什么"),
], ids=["budget", "deposit", "nonfinancial-slot"])
def test_unsegmented_slot_delivers_current_history_and_reopen(
        chat, monkeypatch, entity, attribute, old_value, new_value, question):
    old = chat.say(f"自2021-01-01起，{entity}{attribute}是{old_value}。")
    new = chat.say(f"自2021-03-01起，{entity}{attribute}改为{new_value}。")
    current = f"{entity}现在的{attribute}是{question}？"
    historical = f"在2021-02-01，{entity}{attribute}是{question}？"
    assert requested_attribute(current) == ""
    engine = chat.runtime.memory.recall_engine
    original = engine._non_exact_grounding_score
    reasons = []

    def observe(**kwargs):
        result = original(**kwargs)
        reasons.append(result[1])
        return result

    monkeypatch.setattr(engine, "_non_exact_grounding_score", observe)
    delivered = chat.service.prefetch(channel="hermes", scope=SCOPE, query=current, limit=8,
                                      task_context={"source_ids": ["hermes"]})
    assert delivered["ok"] is True
    assert delivered["bundle"]["retrieval_status"] in {"evidence_found", "degraded"}
    assert_evidence(delivered["bundle"]["items"], value=new_value, source_id=new, excluded=(old_value,))
    assert new_value in delivered["context"] and old_value not in delivered["context"]
    assert "canonical_temporal_slot" in reasons
    assert_evidence(chat.recall(historical), value=old_value, source_id=old, excluded=(new_value,))
    chat.close()
    chat.open()
    assert_evidence(chat.recall(current), value=new_value, source_id=new, excluded=(old_value,))
    assert_evidence(chat.recall(historical), value=old_value, source_id=old, excluded=(new_value,))


def test_expanded_member_gets_its_own_bound_receipt(chat):
    seed(chat)
    old = next(r for r in children(chat) if "10万元" in r.summary)
    request = request_for("青岚项目现在的预算是多少？")
    resolution = chat.runtime.memory._ordinary_temporal_candidates(request, [old])
    assert len(resolution.items) == len(resolution.matches) == 1
    current = resolution.items[0]
    assert "12万元" in current.summary and current.record_id != old.record_id
    assert resolution.supports(request, query=request.query, record=current, text=current.summary)
    assert not resolution.supports(request, query=request.query, record=old, text=old.summary)
    engine = chat.runtime.memory.recall_engine
    args = dict(query=request.query, item=current, evidence=set(), component_hints_by_ref={},
                graph_grounded_ids=set(), vector_min_score=0.12, explicit_recall_boundary=False)
    assert engine._non_exact_grounding_score(**args)[0] < engine._relevance_selector_thresholds["non_exact_min_grounding"]
    score, reason = engine._non_exact_grounding_score(**args,
        temporal_support=lambda query, item, text: resolution.supports(
            request, query=query, record=item, text=text))
    assert reason == "canonical_temporal_slot"
    assert score == engine._relevance_selector_thresholds["non_exact_min_grounding"]


def test_receipt_binds_request_query_partition_digest_quote_and_valid_time(chat):
    seed(chat)
    request = request_for("在2021-02-01，青岚项目预算是多少？")
    resolution = chat.runtime.memory._ordinary_temporal_candidates(request, children(chat))
    assert len(resolution.matches) == 1
    record, proof = resolution.items[0], resolution.matches[0]
    assert "10万元" in record.summary
    assert proof.valid_at == "2021-02-01T00:00:00+00:00"
    assert proof.family_id and proof.revision_id and proof.head_revision
    assert proof.supports(request, query=request.query, record=record, text=record.summary)
    assert not proof.supports(replace(request), query=request.query, record=record, text=record.summary)
    assert not proof.supports(request, query="青岚项目现在的预算是多少？", record=record, text=record.summary)
    assert not proof.supports(request, query=request.query, record=record, text="10万元")
    variants = [
        replace(record, record_id=record.record_id + "0"),
        replace(record, source_id="codex"),
        replace(record, scope=replace(record.scope, user_id="other-owner")),
        replace(record, scope=replace(record.scope, workspace_id="other-workspace")),
        replace(record, title=record.title + " changed"),
        replace(record, meta={**record.meta, "untrusted_admission": True}),
        replace(record, summary=record.summary.replace("10万元", "99万元")),
    ]
    for changed in variants:
        assert not proof.supports(request, query=request.query, record=changed, text=changed.summary)
    with pytest.raises(FrozenInstanceError):
        proof.valid_at = "2021-04-01T00:00:00+00:00"


@pytest.mark.parametrize("question", [
    "node07现在的区域是什么？", "node0070现在的区域是什么？",
    "anode007现在的区域是什么？", "node007现在的区域编码是什么？",
], ids=["leading-zero", "numeric-suffix", "id-substring", "wrong-slot"])
def test_temporal_grounding_does_not_admit_nearby_identity_or_slot(chat, question):
    chat.say("自2021-01-01起，node007区域是East。")
    assert chat.recall(question) == []


@pytest.mark.parametrize("text", [
    "自2021-01-01起，青岚项目预算不是12万元。",
    "自2021-01-01起，计划将青岚项目预算改为12万元。",
    "自2021-01-01起，建议青岚项目预算改为12万元。",
    "自2021-01-01起，青岚项目预算改为12万元（如果获批）。",
    "自2021-01-01起，青岚项目预算改为12万元（待审批）。",
    "自2021-01-01起，青岚项目预算改为12万元（获批后）。",
    "自2021-01-01起，青岚项目预算是待定。",
], ids=["negated", "planned", "proposed", "conditional-value", "pending-value", "approval-condition", "unknown-value"])
def test_nonfactual_assertion_does_not_gain_generic_slot_admission(chat, text):
    chat.say(text)
    assert chat.recall("青岚项目现在的预算是多少？") == []


def test_value_modality_guard_does_not_interpret_object_names():
    text = "自2021-01-01起，计划局的区域是East。"
    grounded = ground_assertion(text, text)
    assert grounded["state"] == "resolved"
    assert grounded["fact"]["entity_id"] == "计划局"


def test_literal_value_names_and_states_are_not_modal_qualifiers():
    for attribute, value in (("归属部门", "计划局"), ("status", "pending"),
                             ("归属部门", "“North (Ops)”")):
        text = f"自2021-01-01起，设备007的{attribute}是{value}。"
        grounded = ground_assertion(text, text)
        assert grounded["state"] == "resolved"
        assert grounded["value"] == value


def test_conditional_value_cannot_replace_known_nonfinancial_slot(chat):
    old = chat.say("自2021-01-01起，星棠设备归属地是北库。")
    chat.say("自2021-03-01起，星棠设备归属地改为南库（如果获批）。")
    assert_evidence(chat.recall("星棠设备现在的归属地是什么？"), value="北库",
                    source_id=old, excluded=("南库",))


def test_temporal_tag_and_caller_flags_cannot_replace_canonical_evidence(chat):
    text = "自2021-01-01起，青岚项目预算是99万元。"
    forged = chat.runtime.memory.ingest(text=text, title=text, memory_type="fact",
        scope={**scoped(), "preserve_scope": True}, source="hermes.l1", source_id="hermes",
        force_capture=True, meta={KEY: ground_assertion(text, text), "authoritative": True,
            "memory_layer": "l1", "runtime_channel": "hermes", "capture_origin": "l1_extract"})
    request = request_for("青岚项目现在的预算是多少？")
    result = chat.runtime.memory._ordinary_temporal_candidates(request, [forged])
    assert not result.items and not result.matches
    bundle = chat.runtime.memory.recall(query=request.query, scope=scoped(),
        task_context={"source_ids": ["hermes"], "temporal_support": True,
                      "include_admission": True, KEY: {"state": "resolved"}})
    assert bundle.items == []


def test_final_temporal_filter_cannot_borrow_same_reference_digest(chat):
    seed(chat)
    request = request_for("青岚项目现在的预算是多少？")
    result = chat.runtime.memory._ordinary_temporal_candidates(request, children(chat))
    record = result.items[0]
    changed = replace(record, title=record.title + " changed")
    assert resolve_candidates(chat.runtime.memory, request, [record, changed], expand=False) == [record]


def test_source_change_after_selection_revokes_delivered_evidence(chat, monkeypatch):
    _, parent_id = seed(chat)
    engine = chat.runtime.memory.recall_engine
    original = engine._select_post_fusion_items
    observed = []

    def select_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.extend(result[0])
        def mutate(sqlite):
            parent = sqlite.get_by_exact_ref(parent_id, scope=ScopeRef.from_dict(scoped()), source_id="hermes")
            parent.content["text"] += " Altered source."
            sqlite.upsert(parent, commit=False)
            return None, [parent], []
        chat.runtime.store.mutate_records_atomically(mutate)
        return result

    monkeypatch.setattr(engine, "_select_post_fusion_items", select_then_change)
    assert chat.recall("青岚项目现在的预算是多少？") == []
    assert any("12万元" in r.summary for r in observed)


def test_new_journal_operation_after_selection_is_subtractive(chat, monkeypatch):
    seed(chat)
    engine = chat.runtime.memory.recall_engine
    original = engine._select_post_fusion_items
    changed = []

    def select_then_transition(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            assert any("12万元" in r.summary for r in result[0])
            changed.append(chat.say("自2021-05-01起，青岚项目预算改为14万元。"))
        return result

    monkeypatch.setattr(engine, "_select_post_fusion_items", select_then_transition)
    assert chat.recall("青岚项目现在的预算是多少？") == []
    assert_evidence(chat.recall("青岚项目现在的预算是多少？"), value="14万元",
                    source_id=changed[0], excluded=("10万元", "12万元"))


def test_concurrent_hybrid_requests_keep_proofs_and_policy_local(chat, monkeypatch):
    seed(chat)
    memory = chat.runtime.memory
    engine = memory.recall_engine
    admission = RelevanceAdmission(RelevanceConfig())
    monkeypatch.setattr(engine, "relevance_admission", admission)
    original = engine._non_exact_grounding_score
    rendezvous = Barrier(2)

    def concurrent_grounding(**kwargs):
        assert engine.relevance_admission is admission
        rendezvous.wait(timeout=10)
        return original(**kwargs)

    monkeypatch.setattr(engine, "_non_exact_grounding_score", concurrent_grounding)
    queries = ["青岚项目现在的预算是多少？", "在2021-02-01，青岚项目预算是多少？"]
    def recall(query):
        return memory.recall(query=query, scope=scoped(), task_context={"source_ids": ["hermes"]})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(recall, queries))
    for bundle, expected, excluded in zip(results, ["12万元", "10万元"], ["10万元", "12万元"]):
        assert any(expected in r.summary for r in bundle.items)
        assert all(excluded not in r.summary for r in bundle.items)
        assert bundle.explanation["relevance_selector"]["hybrid_policy"] == "grounded_backoff_kept_grounded"
    assert engine.relevance_admission is admission


@pytest.mark.parametrize("amount", ["1,800元", "1,800.50元", "1,800,000元"],
                         ids=["grouped", "decimal", "multiple-groups"])
def test_monetary_grouping_preserves_state_and_completed_changes(amount):
    for verb in ("为", "已经改为", "已经调整为", "已经变更为"):
        assert supports_answer_requirements("项目 Linden 的预算多少钱？",
            f"项目 Linden 已备案，预算{verb}{amount}。")


@pytest.mark.parametrize("evidence", [
    "如果获批,项目 Linden 预算改为1,800元。",
    "项目 Linden 预算改为1,800元,尚未实施。",
    "项目 Linden 预算未改为1,800元。",
    "建议项目 Linden 预算改为1,800元。",
    "项目 Linden 已备案,项目 Cedar 预算改为1,800元。",
    "项目 Linden 预付款改为1,800元。",
    "项目 Linden 预算改为1,预付款800元。",
    "项目 Linden 预算改为,1,800元。",
], ids=["conditional", "not-implemented", "negated", "proposed", "wrong-project",
        "wrong-role", "unbound-role", "unbound-amount"])
def test_monetary_grouping_retains_assertion_and_clause_boundaries(evidence):
    assert not supports_answer_requirements("项目 Linden 的预算多少钱？", evidence)
