"""Portable synthetic review boundaries; parent runs these under containment.

The configured model boundary is replaced by deterministic local DTOs. No
external-model correctness or runtime result is claimed by writing this file.
"""
from copy import deepcopy
from hashlib import sha256
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from test_ordinary_chat_temporal_semantics import SCOPE, SourceEchoModel, chat
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.contracts.l1_source_order import l1_source_material_digest
from eimemory.knowledge.chat_temporal import (
    KEY, NAMESPACE, SLOT_NAMESPACE, _query_matches, _source_contradicts, ground_assertion,
)
from eimemory.contracts.temporal_facts import parse_instant
from eimemory.knowledge.sediment import L1ExtractorUnavailable
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef
from eimemory.retrieval.answer_requirements import supports_answer_requirements
from eimemory.retrieval.contracts import CandidateRequest


def slot_fact(slot):
    return {"namespace": SLOT_NAMESPACE, "entity_id": slot, "attribute": "literal-slot"}


def literal_proposal(text, lhs, value, date, operation="claim"):
    def literal(part):
        start = text.index(part)
        return {"text": part, "span": [start, start + len(part)]}
    return {"source_digest": sha256(text.encode()).hexdigest(), "slot": literal(lhs),
            "value": literal(value), "valid_from": literal(date), "valid_until": None,
            "operation": operation}


def test_repeated_current_markers_have_bounded_literal_matching():
    slot = "甲" + "现在的" * 60 + "乙"
    query = "甲" + "现在的" * 90 + "丙是什么？"
    assert not _query_matches(query, slot_fact(slot))
    assert _query_matches(slot + "是什么？", slot_fact(slot))


def test_literal_marker_words_cannot_be_removed_from_identity():
    fact = slot_fact("锦现在的桥负责人")
    assert _query_matches("锦现在的桥现在的负责人是谁？", fact)
    assert not _query_matches("锦桥现在的负责人是谁？", fact)
    quoted = slot_fact("“锦现在的桥”负责人")
    assert _query_matches("“锦现在的桥”现在的负责人是谁？", quoted)
    assert not _query_matches("“锦现在的桥现在的”负责人是谁？", quoted)


def test_query_matching_preserves_ids_word_boundaries_and_envelope():
    fact = {"namespace": NAMESPACE, "entity_id": "node007", "attribute": "retry interval"}
    assert _query_matches("What is the current node007 retry   interval?", fact)
    assert _query_matches("As of 2021-03-01, what was node007 retry interval?", fact)
    for query in ("node07 retry interval?", "node0070 retry interval?",
                  "anode007 retry interval?", "Node007 retry interval?",
                  "node007retry interval?", "node007 retry intervalnow?",
                  "node007 retry interval for another device?"):
        assert not _query_matches(query, fact), query


def test_query_match_work_and_deadline_exhaustion_fail_closed():
    fact = slot_fact("云杉项目负责人")
    query = "云杉项目现在的负责人是谁？"
    assert not _query_matches(query, fact, max_work=0)
    assert not _query_matches(query, fact, deadline=perf_counter() - 1)
    assert not _query_matches("甲" * 4097, fact)
    assert not _query_matches(query, slot_fact("甲" * 513))


@pytest.mark.parametrize("verb", ["为", "约为", "", "改为", "调整为", "变更为"],
                         ids=["state", "approximate", "implicit", "changed", "adjusted", "amended"])
def test_grouped_money_binds_owner_and_modality_for_every_assertion(verb):
    query = "项目 Linden 的预算多少钱？"
    for amount in ("1,800元", "1,800.50元", "1,800,000元"):
        assert supports_answer_requirements(query, f"项目 Linden 已备案，预算{verb}{amount}。")
        for text in (f"项目 Linden 已备案，项目 Cedar 预算{verb}{amount}。",
                     f"项目 Cedar 预算{verb}{amount}，项目 Linden 已备案。",
                     f"如果获批，项目 Linden 预算{verb}{amount}。",
                     f"项目 Linden 预算{verb}{amount}，前提是获批。",
                     f"项目 Linden 预算{verb}{amount}，对吗？",
                     f"项目 Linden 预付款{verb}{amount}。"):
            assert not supports_answer_requirements(query, text), text


def test_money_legacy_currency_alias_and_contract_antecedents():
    query = "项目 Linden 的预算多少钱？"
    for amount in ("USD 1800", "CNY 1800", "￥860", "十二万元"):
        assert supports_answer_requirements(query, f"项目 Linden 预算为{amount}，验收后付款。")
    assert supports_answer_requirements(query, "预算为1,800元。", aliases=["Linden"])
    contract = "项目 Linden 合同总金额多少钱？"
    assert supports_answer_requirements(contract, "项目 Linden 合同已签订。该合同总价为1,800元。")
    assert not supports_answer_requirements(contract,
        "项目 Linden 已备案，项目 Cedar 合同已签订。该合同总价为1,800元。")
    assert not supports_answer_requirements(contract,
        "如果获批，项目 Linden 合同将签订。该合同总价为1,800元。")


def test_money_other_role_unknown_and_literal_names_are_not_requested_modality():
    for suffix in ("预付款未知", "尾款尚未支付"):
        assert supports_answer_requirements("项目 Linden 的预算多少钱？",
            f"项目 Linden 预算为860元，{suffix}。")
    assert supports_answer_requirements("项目 未央 的预算多少钱？", "项目 未央 预算为860元。")
    assert not supports_answer_requirements("项目 未央 的预算多少钱？", "如果获批，项目 未央 预算为860元。")
    assert not supports_answer_requirements("项目 Linden 的预算多少钱？",
        "预付款确认后，项目 Linden 预算为860元。")
    assert not supports_answer_requirements("项目 如果 的预算多少钱？",
        "如果获批，项目 如果 预算为1,800元。")
    assert not supports_answer_requirements("项目 如果 的预算多少钱？",
        "如果预算获批，项目 如果 预算为1,800元。")
    assert not supports_answer_requirements("项目 Linden 的预算多少钱？",
        "如果获批，预算为1,800元。", aliases=["Linden", "如果"])


@pytest.mark.parametrize("value", ['“Unknown (Ops)”', '"Unknown (Ops)"', '“Maybe (North)”'],
                         ids=["curly", "straight", "literal-maybe"])
def test_quoted_literal_values_keep_exact_source_spans_and_digest(value):
    text = f"自2021-01-01起，设备007的归属部门是{value}。"
    proposed = literal_proposal(text, "设备007的归属部门", value, "2021-01-01")
    for dto in (None, proposed):
        grounded = ground_assertion(text, text, dto)
        assert grounded["state"] == "resolved"
        assert grounded["value"] == value and grounded["quote"] == text
        assert grounded["user_digest"] == sha256(text.encode()).hexdigest()
        field = grounded["proposal"]["value"]
        assert text[slice(*field["span"])] == field["text"] == value
    legacy = deepcopy(proposed)
    del legacy["slot"]
    for key, part in (("subject", "设备007"), ("attribute", "归属部门")):
        at = text.index(part)
        legacy[key] = {"text": part, "span": [at, at + len(part)]}
    assert ground_assertion(text, text, legacy)["state"] == "resolved"


def test_quoted_value_does_not_hide_outside_doubt_or_malformed_delimiters():
    for text in (
        "自2021-01-01起，设备007的归属部门可能是“Unknown (Ops)”。",
        "自2021-01-01起，设备007的归属部门不是“Unknown (Ops)”。",
        "计划：自2021-01-01起，设备007的归属部门是“Unknown (Ops)”。",
        "自2021-01-01起，设备007的归属部门是Unknown (Ops)。",
        "自2021-01-01起，设备007的归属部门是“Unknown (Ops)。",
        "自2021-01-01起，设备007的归属部门是Unknown (Ops)”。",
        '自2021-01-01起，设备007的归属部门是“Unknown (Ops)"。',
    ):
        assert ground_assertion(text, text)["state"] == "unresolved", text
    text = "自2021-01-01起，设备007的归属部门是“Unknown (Ops)”。"
    partial = literal_proposal(text, "设备007的归属部门", "Unknown (Ops)", "2021-01-01")
    assert ground_assertion(text, text, partial)["state"] == "unresolved"


def model_result(monkeypatch, response):
    original = SourceEchoModel.complete

    def complete(self, **kwargs):
        if kwargs["user_prompt"].startswith("{"):
            return original(self, **kwargs)
        return SimpleNamespace(text=response)

    monkeypatch.setattr(SourceEchoModel, "complete", complete)


def broken_response(text, mode):
    if mode == "omitted":
        return "[]"
    if mode == "invalid-root":
        return '{"unexpected":true}'
    if mode == "invalid-json":
        return "not JSON"
    value = "乙员"
    dto = literal_proposal(text, "梧桐项目负责人", value, "2021-06-01", "transition")
    if mode == "overlap":
        del dto["slot"]
        for key, part in (("subject", "梧桐项目"), ("attribute", "项目负责人")):
            at = text.index(part)
            dto[key] = {"text": part, "span": [at, at + len(part)]}
    elif mode == "digest":
        dto["source_digest"] = "0" * 64
    elif mode == "foreign-field":
        dto["scope"] = {"user_id": "invented"}
    elif mode == "null":
        dto = None
    return json.dumps([{"content": text, "type": "fact", "priority": 90, "temporal": dto}],
                      ensure_ascii=False)


def assert_exact_evidence(chat, items, value, parent_id):
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))
    parent = chat.runtime.store.get_by_exact_ref(parent_id, scope=scope, source_id="hermes")
    selected = [item for item in items if value in item["summary"]]
    assert selected
    for item in selected:
        record = chat.runtime.store.get_by_exact_ref(item["record_id"], scope=scope, source_id="hermes")
        assert record is not None and record.record_id != parent_id
        binding = business_metadata(record.meta)["source_event_binding"]
        assert binding["record_id"] == parent_id
        assert binding["input_digest"] == l1_source_material_digest(parent)
        assert any(link.relation == "derived_from" and link.target_id == parent_id for link in record.links)
        grounded = business_metadata(record.meta)[KEY]
        assert grounded["quote"] == record.summary == item["summary"]
        user = parent.content["text"].split("User: ", 1)[1].split("\nAssistant:", 1)[0]
        assert grounded["user_digest"] == sha256(user.encode()).hexdigest()


@pytest.mark.parametrize("mode", ["overlap", "digest", "foreign-field", "null", "omitted", "invalid-root", "invalid-json"])
def test_unresolved_new_original_withholds_old_current_and_restores_later(chat, monkeypatch, mode):
    old = chat.say("自2021-01-01起，梧桐项目负责人是甲员。")
    text = "自2021-06-01起，梧桐项目负责人改为乙员。"
    with monkeypatch.context() as patch:
        model_result(patch, broken_response(text, mode))
        if mode in {"invalid-root", "invalid-json"}:
            with pytest.raises(L1ExtractorUnavailable):
                chat.say(text)
        else:
            chat.say(text)
    for reopened in (False, True):
        if reopened:
            chat.close()
            chat.open()
        current = chat.recall("梧桐项目现在的负责人是谁？")
        assert all("甲员" not in item["summary"] and "乙员" not in item["summary"] for item in current)
        assert_exact_evidence(chat, chat.recall("在2021-03-01，梧桐项目负责人是谁？"), "甲员", old)
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))
    records = chat.runtime.store.list_records(kinds=["memory"], scope=scope, source_ids=["hermes"], limit=100)
    assert not any(business_metadata(r.meta).get(KEY, {}).get("state") == "resolved"
                   and "乙员" in r.summary for r in records)
    new = chat.say("自2021-09-01起，梧桐项目负责人改为丙员。")
    assert_exact_evidence(chat, chat.recall("梧桐项目现在的负责人是谁？"), "丙员", new)
    assert all("甲员" not in item["summary"] for item in chat.recall("梧桐项目现在的负责人是谁？"))


def test_omitted_new_original_is_checked_even_with_only_old_candidate(chat, monkeypatch):
    chat.say("自2021-01-01起，桐影设备归属地是北库。")
    scope = resolve_channel_scope("hermes", SCOPE)
    old = next(r for r in chat.runtime.store.list_records(kinds=["memory"], scope=ScopeRef.from_dict(scope),
        source_ids=["hermes"], limit=100) if business_metadata(r.meta).get(KEY, {}).get("state") == "resolved")
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        chat.say("自2021-06-01起，桐影设备归属地改为南库。")
    request = CandidateRequest.create(query="桐影设备现在的归属地是什么？", scope=scope, source_ids=["hermes"])
    result = chat.runtime.memory._ordinary_temporal_candidates(request, [old])
    assert result.items == result.matches == ()


def test_source_uncertainty_uses_business_dates_and_explicit_interval(chat, monkeypatch):
    old = chat.say("自2021-01-01起，松林设备归属地是北库。")
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        chat.say("从2021-06-01到2021-07-01，松林设备归属地是南库。")
    assert_exact_evidence(chat, chat.recall("松林设备现在的归属地是什么？"), "北库", old)
    assert chat.recall("在2021-06-15，松林设备归属地是什么？") == []
    assert_exact_evidence(chat, chat.recall("在2021-07-01，松林设备归属地是什么？"), "北库", old)


def test_earlier_capture_later_business_change_is_still_uncertain(chat, monkeypatch):
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        chat.say("自2021-06-01起，松林设备归属地改为南库。")
    old = chat.say("自2021-01-01起，松林设备归属地是北库。")
    assert chat.recall("松林设备现在的归属地是什么？") == []
    assert_exact_evidence(chat, chat.recall("在2021-03-01，松林设备归属地是什么？"), "北库", old)


def test_omitted_other_slots_same_value_unknown_dates_and_future_do_not_block(chat, monkeypatch):
    old = chat.say("自2021-01-01起，松林设备归属地是北库。")
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        for text in ("自2021-06-01起，杉木设备归属地改为南库。",
                     "自2021-06-01起，松林设备归属部门改为南部。",
                     "自2021-06-01起，松林设备归属地是北库。",
                     "松林设备归属地改为南库，日期不详。",
                     "自2021-06-01起，松林设备归属地可能是南库。",
                     "自2099-06-01起，松林设备归属地改为南库。"):
            chat.say(text)
    assert_exact_evidence(chat, chat.recall("松林设备现在的归属地是什么？"), "北库", old)
    assert chat.recall("在2099-06-01，松林设备归属地是什么？") == []


def test_omitted_other_scope_channel_or_assistant_never_blocks(chat, monkeypatch):
    old = chat.say("自2021-01-01起，松林设备归属地是北库。")
    changed = "自2021-06-01起，松林设备归属地改为南库。"
    cases = [("codex", SCOPE, changed, "Acknowledged.")]
    for column in ("tenant_id", "agent_id", "workspace_id", "user_id"):
        cases.append(("hermes", {**SCOPE, column: "different"}, changed, "Acknowledged."))
    cases.append(("hermes", SCOPE, "Hello.", changed))
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        for index, (channel, scope, user, assistant) in enumerate(cases):
            result = chat.service.sync_turn(channel=channel, scope=scope, session_id="other-source",
                turn_id=str(index), user_text=user, assistant_text=assistant)
            assert result["ok"]
    assert_exact_evidence(chat, chat.recall("松林设备现在的归属地是什么？"), "北库", old)


def test_unparseable_unrelated_original_does_not_suppress_known_slot(chat, monkeypatch):
    old = chat.say("自2021-01-01起，松林设备归属地是北库。")
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        patch.setattr(chat.service, "max_turn_chars", 128)
        result = chat.service.sync_turn(channel="hermes", scope=SCOPE, session_id="truncated",
            turn_id="1", user_text="无关闲聊" * 100, assistant_text="Acknowledged.")
        assert result["ok"]
    assert_exact_evidence(chat, chat.recall("松林设备现在的归属地是什么？"), "北库", old)


def test_certified_masked_statement_is_not_an_unresolved_barrier():
    old_text = "自2020-01-01起，松林设备归属地是北库。"
    new_text = "自2022-01-01起，松林设备归属地改为南库。"
    corrected_text = "更正：从2021-01-01到2024-01-01，松林设备归属地是中库。"
    newer = ground_assertion(new_text, new_text)
    corrected = ground_assertion(corrected_text, corrected_text)
    assert newer["state"] == corrected["state"] == "resolved"
    originals = [("source-newer", newer["user_digest"], newer)]
    at = parse_instant("2023-01-01T00:00:00+00:00")
    assert _source_contradicts(corrected, originals, at, set())
    certified = {("source-newer", newer["user_digest"], newer["quote"])}
    assert not _source_contradicts(corrected, originals, at, certified)
    # Certifying a different clause of the same original cannot hide omission.
    certified = {("source-newer", newer["user_digest"], old_text)}
    assert _source_contradicts(corrected, originals, at, certified)


def test_omitted_second_same_slot_clause_retains_original_source_uncertainty(chat, monkeypatch):
    old = chat.say("自2020-01-01起，梧桐项目负责人是甲员。")
    text = "自2021-01-01起，梧桐项目负责人是甲员。自2022-01-01起，梧桐项目负责人改为乙员。"
    with monkeypatch.context() as patch:
        model_result(patch, "[]")
        chat.say(text)
    assert chat.recall("梧桐项目现在的负责人是谁？") == []
    assert_exact_evidence(chat, chat.recall("在2020-06-01，梧桐项目负责人是谁？"), "甲员", old)
