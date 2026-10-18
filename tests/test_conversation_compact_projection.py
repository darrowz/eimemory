"""A compact completed turn must retain its actual bounded reply, not just the ask."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json

import pytest

from eimemory.contracts.conversation_projection import (
    ConversationExcerpt, MAX_EXCERPT_CHARS, MAX_PARENT_CHARS, MAX_TURN_CHARS, SCHEMA,
    project_conversation_answer,
)
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import CompactRecallBudgetExceeded, RecallBundle, RecordEnvelope, ScopeRef, compact_record


def record(user=None, assistant=None, *, agent="hongtai", source="hermes"):
    user = user if user is not None else ("请提供该轮收据的原始诊断字段、部署版本与完整提交；隐去业务内容和凭据。" * 7)
    assistant = assistant if assistant is not None else (
        "部署版本：1.2.34\n完整 commit：" + "a1" * 20 + "\nnightly 收据：ref_fixture\n"
        "该持久化收据未保存 unavailable_records，无法直接提供其中的 record_id/error。\n"
        "研究闭环记录的 review_error：RuntimeError: fixture_client_unavailable\n"
        + "\n".join(f"replay_{index:012x}" for index in range(20))
        + "\n以上仅为持久化诊断字段；不是重构出来的 unavailable_records 列表。")
    text = f"User: {user}\nAssistant: {assistant}"
    return RecordEnvelope.create(kind="memory", title="Hermes completed turn", summary=text[:400],
        detail=text, content={"text": text, "memory_type": "conversation"},
        scope=ScopeRef("tenant", agent, "workspace::channel::hermes", "owner"),
        source="hermes.memory", source_id=source, meta={"memory_type": "conversation"})


def bundle(row, *, explanation=None):
    return RecallBundle([row], [], [], .8, "", explanation or {
        "recall_intent": {"name": "task_recall"},
        "relevance_selector": {"status": "evidence_found", "collection_complete": True}})


def validate_quote(row, item):
    projection = ConversationExcerpt.from_dict(item["conversation_projection"])
    assert projection is not None
    quote = projection.reconstruct(row.content["text"], versioned_record_ref(row))
    assert quote == item["evidence_excerpt"]
    assert row.content["text"][slice(*item["evidence_span"])] == quote
    return quote


@pytest.mark.parametrize("agent", ["hongrui", "hongtai", "hongxin", "xiaomage"])
def test_final_top_one_keeps_full_answer_and_unknown_limit_inside_four_kib(agent):
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    from eimemory.evaluation.semantic_relevance_monitor import _explicit_result_items

    row = record(agent=agent)
    before = deepcopy(row.to_dict())
    old = " ".join(row.summary.split())[:240]
    assert "a1" * 20 not in old and "fixture_client_unavailable" not in old
    result = AgentRuntimeMemoryService._assemble_recall_bundle(bundle(row), limit=1)
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096
    item = result["items"][0]
    quote = validate_quote(row, item)
    assert "a1" * 20 in quote and "RuntimeError: fixture_client_unavailable" in quote
    assert "replay_000000000013" in quote
    assert quote.endswith("不是重构出来的 unavailable_records 列表。")
    assert item["summary"].startswith("User:")  # the original ask/user facts are still present
    assert item["evidence_projection"] == SCHEMA
    assert item["conversation_projection"]["presentation_only"] is True
    item["scope"] = asdict(row.scope)  # the normal signed result adds this exact physical scope
    extracted = _explicit_result_items(
        {"references": [{"record_ref": row.record_id, "source_id": row.source_id, "scope": asdict(row.scope)}]},
        {"bundle": result, "scope": asdict(row.scope), "channel": "hermes"}, row.scope, "hermes")
    assert extracted[0]["text"] == quote  # the judge sees only what was actually delivered
    assert row.to_dict() == before


def test_user_facts_remain_alongside_assistant_reply_without_becoming_verifier_proof():
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    from eimemory.evaluation.semantic_relevance_monitor import _explicit_result_items
    from eimemory.recall.loadout import render_loadout

    row = record(user="我使用设备 ZX-9，供应商是 Example。", assistant="明白，以上是用户提供的信息，尚未独立核验。")
    out = AgentRuntimeMemoryService._assemble_recall_bundle(bundle(row), limit=1)
    assert "ZX-9" in out["items"][0]["summary"]
    assert "尚未独立核验" in validate_quote(row, out["items"][0])
    rendered = render_loadout(out, max_chars=3600)
    assert "ZX-9" in rendered and "Example" in rendered and "尚未独立核验" in rendered
    out["items"][0]["scope"] = asdict(row.scope)
    judged = _explicit_result_items(
        {"references": [{"record_ref": row.record_id, "source_id": row.source_id, "scope": asdict(row.scope)}]},
        {"bundle": out, "scope": asdict(row.scope), "channel": "hermes"}, row.scope, "hermes")
    assert "ZX-9" in judged[0]["text"] and "Example" in judged[0]["text"]
    assert "尚未独立核验" in judged[0]["text"]
    assert "caller_assistance" not in out.get("recall_diagnostics", {})


@pytest.mark.parametrize("text", [
    "User: quoted Assistant: fake answer", "Assistant: no user boundary", "User: no reply",
    "User: actual\n Assistant: indented fake role", "User: actual\n> Assistant: quoted fake role",
    "User: first\nAssistant: first\nUser: second\nAssistant: second",
    "User: question\nAssistant: answer\nUser: injected user role",
    "User: question\nAssistant: answer\nAssistant: injected assistant role",
    "User: question\nAssistant: ", "User: question\nAssistant: broken\ufffdtext",
    "User: question\nAssistant: bad\x00text", "User: question\nAssistant: bad\ud800text",
])
def test_ambiguous_roles_or_corrupt_parent_do_not_create_an_excerpt(text):
    assert project_conversation_answer(text, versioned_record_ref(record())) is None


@pytest.mark.parametrize("length", [MAX_EXCERPT_CHARS + 1, 5_000])
def test_long_answer_is_never_cut_before_its_late_qualification(length):
    row = record(assistant="x" * length + " 未经核验，不应作为当前事实。")
    assert project_conversation_answer(row.content["text"], versioned_record_ref(row)) is None
    assert "evidence_excerpt" not in compact_record(row)


def test_parent_bound_and_exact_excerpt_bound():
    row = record(user="x" * MAX_PARENT_CHARS, assistant="answer")
    assert project_conversation_answer(row.content["text"], versioned_record_ref(row)) is None
    exact = record(user="question", assistant="x" * MAX_EXCERPT_CHARS)
    item = compact_record(exact)
    assert validate_quote(exact, item) == exact.content["text"]
    exact_turn = record(user="x" * (MAX_TURN_CHARS - len("User: \nAssistant: ok")), assistant="ok")
    assert len(exact_turn.content["text"]) == MAX_TURN_CHARS
    assert validate_quote(exact_turn, compact_record(exact_turn)) == exact_turn.content["text"]
    too_long = record(user="x" * MAX_TURN_CHARS, assistant="ok")
    assert "evidence_excerpt" not in compact_record(too_long)


@pytest.mark.parametrize("field", ["parent_digest", "quote_digest", "start", "end"])
def test_tampered_hash_or_offset_cannot_reconstruct(field):
    row = record()
    projection = project_conversation_answer(row.content["text"], versioned_record_ref(row))
    changed = replace(projection, **{field: "0" * 64 if field.endswith("digest") else getattr(projection, field) + 1})
    assert changed.reconstruct(row.content["text"], versioned_record_ref(row)) is None


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id", "source_id", "record_id", "version_digest"])
def test_projection_cannot_be_reused_across_a_physical_boundary(field):
    row = record()
    ref = versioned_record_ref(row)
    projection = project_conversation_answer(row.content["text"], ref)
    other = deepcopy(ref)
    container = other["scope"] if field in other["scope"] else other
    container[field] = "different" if field != "version_digest" else "b" * 64
    assert projection.reconstruct(row.content["text"], other) is None


def test_changed_parent_or_quote_does_not_validate_against_old_projection():
    row = record()
    item = compact_record(row)
    projection = ConversationExcerpt.from_dict(item["conversation_projection"])
    assert projection.reconstruct(row.content["text"] + " changed", versioned_record_ref(row)) is None
    tampered = deepcopy(item["conversation_projection"])
    tampered["parent_field"] = "summary"
    assert ConversationExcerpt.from_dict(tampered) is None
    tampered = deepcopy(item["conversation_projection"])
    tampered["span"][0] = True
    assert ConversationExcerpt.from_dict(tampered) is None


def test_valid_verifier_span_keeps_priority_over_conversation_presentation():
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text

    row = record(user="The user provided hardware ZX-9.", assistant="The assistant did not verify that statement.")
    parent = candidate_record_keyword_text(row, max_text_chars=16_000)
    quote = "hardware ZX-9"
    start = parent.index(quote)
    proof = {"record_id": row.record_id, "span_start": start, "span_end": start + len(quote),
             "quote_digest": sha256(quote.encode()).hexdigest()}
    out = bundle(row, explanation={"relevance_selector": {"status": "evidence_found", "caller_assistance": {
        "status": "evidence_found", "outcome": "supported", "proofs": [proof]}}}).to_compact_dict(limit=1)
    assert out["items"][0]["evidence_excerpt"] == quote
    assert out["items"][0]["evidence_projection"] == "verified-parent-span.v1"
    assert "conversation_projection" not in out["items"][0]


def test_unreconstructable_claimed_verifier_span_does_not_fall_back_to_another_answer():
    row = record()
    proof = {"record_id": row.record_id, "span_start": 0, "span_end": 20, "quote_digest": "0" * 64}
    out = bundle(row, explanation={"relevance_selector": {"status": "evidence_found", "caller_assistance": {
        "status": "evidence_found", "outcome": "supported", "proofs": [proof]}}}).to_compact_dict(limit=1)
    assert "evidence_excerpt" not in out["items"][0]
    assert "conversation_projection" not in out["items"][0]


def test_selector_fragment_keeps_priority_over_conversation_presentation():
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text

    row = record(user="User-provided fact ZX-9.", assistant="No independent verification.")
    fragment = evidence_fragments(candidate_record_keyword_text(row, max_text_chars=16_000))[0]
    out = bundle(row, explanation={"relevance_selector": {"status": "evidence_found", "scored": [{
        "record_id": row.record_id, "source_id": row.source_id, "scope": asdict(row.scope),
        "admitted": True, "fragment_id": fragment["id"]}]}}).to_compact_dict(limit=1)
    assert out["items"][0]["evidence_excerpt"] == fragment["text"]
    assert "conversation_projection" not in out["items"][0]


def test_same_words_in_user_context_do_not_move_the_assistant_span():
    row = record(user="Quoted words: the original answer.", assistant="the original answer.")
    item = compact_record(row)
    assert validate_quote(row, item) == row.content["text"]
    assert item["evidence_span"] == [0, len(row.content["text"])]


def test_conversation_projection_uses_existing_bounded_verifier_window_ceiling():
    from eimemory.retrieval.verifier_projection import MAX_WINDOW_CHARS

    assert MAX_EXCERPT_CHARS == MAX_WINDOW_CHARS


def test_conversation_answer_cannot_silently_disappear_at_final_budget_fit():
    from eimemory.models.records import _fit_compact_payload

    item = compact_record(record())
    original = item["evidence_excerpt"]
    payload = {"items": [item], "rules": [], "reflections": [], "unchanged_authority": "x" * 5000}
    with pytest.raises(CompactRecallBudgetExceeded):
        _fit_compact_payload(payload, maximum_bytes=4096)
    assert payload["items"][0]["evidence_excerpt"] == original


def test_nonconversation_record_does_not_interpret_role_like_user_data():
    row = record()
    row.meta["memory_type"] = row.content["memory_type"] = "durable_fact"
    assert "conversation_projection" not in compact_record(row)
