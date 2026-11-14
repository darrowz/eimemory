"""Owned literal-proposal contracts; parent executes under OS containment.

Only the model boundary is scripted. Positive ordinary-chat checks require
delivered evidence, exact parent lineage and persistence across reopen.
"""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from test_ordinary_chat_temporal_semantics import SCOPE, SourceEchoModel, chat
from test_chat_temporal_reviewfix1 import assert_exact_evidence
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.knowledge.chat_temporal import KEY, _compile_literal_proposal, ground_assertion
from eimemory.knowledge.l1_prompts import extract_memories_system_prompt
from eimemory.knowledge.sediment import L1ExtractorUnavailable
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef


SCHEMA = "ordinary_temporal_literals.v1"
SOURCE = "Since 2020-08-01, engine-0064::sampling period changed to 35 seconds."


def literal_dto(user, slot="engine-0064::sampling period", value="35 seconds",
                start="2020-08-01", until=None, operation="transition"):
    return {"schema": SCHEMA, "source_digest": sha256(user.encode()).hexdigest(),
            "slot": slot, "value": value, "valid_from": start,
            "valid_until": until, "operation": operation}


def legacy_dto(user, dto):
    result = {key: value for key, value in dto.items() if key != "schema"}
    for key in ("slot", "value", "valid_from", "valid_until"):
        value = result[key]
        if value is not None:
            start = user.index(value)
            result[key] = {"text": value, "span": [start, start + len(value)]}
    return result


@pytest.mark.parametrize("user,slot,value,date,operation,entity,attribute", [
    (SOURCE, "engine-0064::sampling period", "35 seconds", "2020-08-01", "transition",
     "engine-0064", "sampling period"),
    ("自2020-08-01起，泉岚项目通知渠道改为短信。", "泉岚项目通知渠道", "短信", "2020-08-01", "transition",
     "泉岚项目通知渠道", "literal-slot"),
    ("自2021年6月1日起，设备-0007的归属部门是“Unknown 🧭 (Ops)”。", "设备-0007的归属部门",
     "“Unknown 🧭 (Ops)”", "2021年6月1日", "claim", "设备-0007", "归属部门"),
    ('Since 2020-08-01, Engine-0007::owner is "Unknown 🧭 (Ops)".', "Engine-0007::owner",
     '"Unknown 🧭 (Ops)"', "2020-08-01", "claim", "Engine-0007", "owner"),
    ("host-008's sector is North effective 2021-06-01.", "host-008's sector", "North",
     "2021-06-01", "claim", "host-008", "sector"),
], ids=["offset-counterexample", "whole-chinese-slot", "unicode-curly-quotes", "case-zeroes-straight-quotes", "suffix-date"])
def test_literal_proposal_resolves_without_mutation(user, slot, value, date, operation, entity, attribute):
    dto = literal_dto(user, slot, value, date, operation=operation)
    original = deepcopy(dto)
    grounded = ground_assertion(user, "A paraphrase cannot supply positions or authority.", dto)
    assert grounded["state"] == "resolved"
    assert grounded["value"] == value and grounded["operation"] == operation
    assert grounded["fact"]["entity_id"] == entity
    assert grounded["fact"]["attribute"] == attribute
    assert grounded["user_digest"] == original["source_digest"]
    assert grounded["quote"] == user
    assert dto == original == grounded["proposal"]
    assert grounded["proposal"] is not dto
    assert ground_assertion(user, grounded["quote"], grounded["proposal"]) == grounded


@pytest.mark.parametrize("updates", [
    {"schema": "ordinary_temporal_literals.v2"}, {"schema": None},
    {"source_digest": "0" * 64}, {"source_digest": False},
    {"operation": "claim"}, {"operation": "update"}, {"operation": []},
    {"scope": {"user_id": "foreign"}}, {"authority": "owner"},
    {"namespace": "external"}, {"source": "codex"}, {"target_ids": ["other"]},
    {"subject": "engine-0064"}, {"attribute": "sampling period"},
    {"span": [18, 46]}, {"value": {"text": "35 seconds", "span": [58, 68]}},
    {"value": "35秒"}, {"slot": "sampling period"},
    {"slot": "engine-064::sampling period"}, {"slot": "Engine-0064::sampling period"},
    {"slot": "engine-0064::sampling period "}, {"slot": ""}, {"value": ""},
    {"valid_from": ""}, {"valid_until": ""}, {"valid_from": 20200801},
    {"valid_until": False}, {"slot": None}, {"value": ["35 seconds"]},
], ids=["unsupported-schema", "schema-type", "bad-hash", "hash-type", "wrong-operation", "unknown-operation",
        "operation-type", "foreign-scope", "authority", "namespace", "source", "target-ids", "subject", "attribute",
        "mixed-span", "mixed-value-object", "translated-value", "partial-slot", "zeroes", "case", "trailing-space",
        "empty-slot", "empty-value", "empty-date", "empty-until", "date-type", "until-type", "slot-type", "value-type"])
def test_literal_proposal_rejects_invalid_fields_unchanged(updates):
    dto = literal_dto(SOURCE)
    dto.update(updates)
    original = deepcopy(dto)
    assert ground_assertion(SOURCE, SOURCE, dto)["state"] == "unresolved"
    assert dto == original


@pytest.mark.parametrize("missing", ["schema", "source_digest", "slot", "value", "valid_from", "valid_until", "operation"])
def test_literal_proposal_requires_complete_exact_keyset(missing):
    dto = literal_dto(SOURCE)
    del dto[missing]
    assert ground_assertion(SOURCE, SOURCE, dto)["state"] == "unresolved"


@pytest.mark.parametrize("field", ["slot", "value", "valid_from", "valid_until"])
def test_literal_proposal_rejects_duplicate_original_occurrences(field):
    clause = "从2020-08-01到2021-08-01，设备007的状态是在线。"
    dto = literal_dto(clause, "设备007的状态", "在线", "2020-08-01", "2021-08-01", "claim")
    assert ground_assertion(clause, clause, dto)["state"] == "resolved"
    user = clause + "备注：" + dto[field] + "。"
    dto["source_digest"] = sha256(user.encode()).hexdigest()
    assert ground_assertion(user, clause, dto)["state"] == "unresolved"


def test_overlapping_occurrences_are_not_a_unique_literal():
    user = "自2021-01-01起，设备007的状态为为为。"
    dto = literal_dto(user, "设备007的状态", "为为", "2021-01-01", operation="claim")
    # The complete RHS also starts at the connective: count() misses overlap.
    assert user.count("为为") == 1
    with pytest.raises(ValueError, match="chat_literal_not_unique"):
        _compile_literal_proposal(user, dto, dto["source_digest"])
    assert ground_assertion(user, user, dto)["state"] == "unresolved"


@pytest.mark.parametrize("user", [
    "Since 2020-08-01, another::mode is on. engine-0064::sampling period changed to 35 seconds.",
    "Since 2020-08-01, engine-0064::sampling period changed to 35 seconds, maybe.",
    "Since 2020-08-01, engine-0064::sampling period changed to 35 seconds and another::mode is on.",
    "Record 2020-08-01: engine-0064::sampling period changed to 35 seconds.",
], ids=["cross-clause", "uncertainty", "multiple-relations", "nonbusiness-date"])
def test_literal_locations_do_not_establish_semantic_proof(user):
    assert ground_assertion(user, user, literal_dto(user))["state"] == "unresolved"


@pytest.mark.parametrize("operation,marker", [("correction", "更正"), ("retraction", "撤回")],
                         ids=["correction", "retraction"])
def test_literal_bounded_operations_keep_interval_and_marker_requirements(operation, marker):
    user = marker + "：从2020-08-01到2021-08-01，设备007的状态是在线。"
    dto = literal_dto(user, "设备007的状态", "在线", "2020-08-01", "2021-08-01", operation)
    grounded = ground_assertion(user, user, dto)
    assert grounded["state"] == "resolved" and grounded["operation"] == operation
    assert grounded["interval"] == {"valid_from": "2020-08-01T00:00:00+00:00",
                                    "valid_until": "2021-08-01T00:00:00+00:00",
                                    "until_kind": "bounded", "precision": "second"}
    without_marker = user.split("：", 1)[1]
    assert ground_assertion(without_marker, without_marker, {**dto, "source_digest": sha256(without_marker.encode()).hexdigest()})["state"] == "unresolved"
    assert ground_assertion(user, user, {**dto, "valid_until": None})["state"] == "unresolved"
    reversed_user = marker + "：从2021-08-01到2020-08-01，设备007的状态是在线。"
    reversed_dto = literal_dto(reversed_user, "设备007的状态", "在线", "2021-08-01", "2020-08-01", operation)
    assert ground_assertion(reversed_user, reversed_user, reversed_dto)["state"] == "unresolved"


def test_literal_matching_limits_refuse_without_truncating_original():
    user = SOURCE + "\n" + "x" * 65_536
    assert ground_assertion(user, SOURCE, literal_dto(user))["state"] == "unresolved"
    value = "x" * 513
    user = "Since 2020-08-01, engine-0064::sampling period changed to " + value + "."
    assert ground_assertion(user, user, literal_dto(user, value=value))["state"] == "unresolved"


@pytest.mark.parametrize("split", [False, True], ids=["slot", "subject-attribute"])
def test_legacy_span_proposals_still_resolve_and_never_repair_bad_offsets(split):
    dto = legacy_dto(SOURCE, literal_dto(SOURCE))
    if split:
        del dto["slot"]
        for key, text in (("subject", "engine-0064"), ("attribute", "sampling period")):
            start = SOURCE.index(text)
            dto[key] = {"text": text, "span": [start, start + len(text)]}
    original = deepcopy(dto)
    assert ground_assertion(SOURCE, SOURCE, dto)["state"] == "resolved"
    field = "attribute" if split else "slot"
    for index, delta in ((0, 1), (1, -1), (1, 1)):
        bad = deepcopy(dto)
        bad[field]["span"][index] += delta
        before = deepcopy(bad)
        assert ground_assertion(SOURCE, SOURCE, bad)["state"] == "unresolved"
        assert bad == before
    for extra in ({"source_digest": "0" * 64}, {"scope": {"user_id": "foreign"}}, {"schema": SCHEMA}):
        assert ground_assertion(SOURCE, SOURCE, {**dto, **extra})["state"] == "unresolved"
    assert dto == original


def scripted_model(monkeypatch, responses):
    original = SourceEchoModel.complete
    calls = []

    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            return original(self, **kwargs)
        user = prompt.split("\nUSER:\n", 1)[1].split("\n\nASSISTANT:\n", 1)[0]
        calls.append(user)
        assert "【原始USER文本SHA256】" + sha256(user.encode()).hexdigest() in prompt
        source_ids = json.loads(prompt.split("【权威来源消息ID】", 1)[1].split("\n", 1)[0])
        assert source_ids
        dto = responses[user]
        if type(dto) is str:
            return SimpleNamespace(text=dto)
        return SimpleNamespace(text=json.dumps([{
            "content": "A synthetic observation paraphrase.", "type": "fact", "priority": 90,
            "source_message_ids": source_ids, "temporal": dto,
        }], ensure_ascii=False))

    monkeypatch.setattr(SourceEchoModel, "complete", complete)
    return calls


def assert_literal_evidence(chat, items, value, parent, dto):
    assert_exact_evidence(chat, items, value, parent)
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))
    for item in items:
        assert item["source_id"] == "hermes"
        if value in item["summary"]:
            record = chat.runtime.store.get_by_exact_ref(item["record_id"], scope=scope, source_id="hermes")
            assert record.scope == scope
            meta = business_metadata(record.meta)
            assert meta["source_message_ids"] == [parent]
            assert meta[KEY]["proposal"] == dto


@pytest.mark.parametrize("old,new,slot,old_value,new_value,current,history", [
    ("Since 2020-01-01, engine-0064::sampling period is 20 seconds.", SOURCE,
     "engine-0064::sampling period", "20 seconds", "35 seconds",
     "What is the current engine-0064 sampling period?", "As of 2020-04-01, what was engine-0064 sampling period?"),
    ("自2020-01-01起，泉岚项目通知渠道是邮件。", "自2020-08-01起，泉岚项目通知渠道改为短信。",
     "泉岚项目通知渠道", "邮件", "短信", "泉岚项目现在的通知渠道是什么？", "在2020-04-01，泉岚项目通知渠道是什么？"),
], ids=["english-offset-counterexample", "chinese-whole-slot"])
def test_literal_model_dto_ordinary_current_history_parent_and_reopen(chat, monkeypatch, old, new, slot, old_value, new_value, current, history):
    old_dto = literal_dto(old, slot, old_value, "2020-01-01", operation="claim")
    new_dto = literal_dto(new, slot, new_value, "2020-08-01")
    calls = scripted_model(monkeypatch, {old: old_dto, new: new_dto})
    old_parent, new_parent = chat.say(old), chat.say(new)
    for reopened in (False, True):
        if reopened:
            chat.close()
            chat.open()
        now = chat.recall(current)
        assert_literal_evidence(chat, now, new_value, new_parent, new_dto)
        assert all(old_value not in item["summary"] for item in now)
        earlier = chat.recall(history)
        assert_literal_evidence(chat, earlier, old_value, old_parent, old_dto)
        assert all(new_value not in item["summary"] for item in earlier)
    assert calls == [old, new]


@pytest.mark.parametrize("mode", ["bad-hash", "foreign-scope", "mixed-span", "wrong-operation", "legacy-offbyone",
                                  "null", "omitted", "invalid-json", "missing-output"])
def test_invalid_new_output_withholds_current_preserves_history_and_reopen(chat, monkeypatch, mode):
    old = "Since 2020-01-01, engine-0064::sampling period is 20 seconds."
    old_dto = literal_dto(old, value="20 seconds", start="2020-01-01", operation="claim")
    new_dto = literal_dto(SOURCE)
    if mode == "bad-hash":
        new_dto["source_digest"] = "0" * 64
    elif mode == "foreign-scope":
        new_dto["scope"] = {"user_id": "foreign"}
    elif mode == "mixed-span":
        new_dto["slot"] = legacy_dto(SOURCE, new_dto)["slot"]
    elif mode == "wrong-operation":
        new_dto["operation"] = "claim"
    elif mode == "legacy-offbyone":
        new_dto = legacy_dto(SOURCE, new_dto)
        new_dto["slot"]["span"][1] += 1
    elif mode == "null":
        new_dto = None
    elif mode == "omitted":
        new_dto = "[]"
    elif mode == "invalid-json":
        new_dto = "not JSON"
    elif mode == "missing-output":
        new_dto = ""
    calls = scripted_model(monkeypatch, {old: old_dto, SOURCE: new_dto})
    old_parent = chat.say(old)
    current = "What is the current engine-0064 sampling period?"
    assert_literal_evidence(chat, chat.recall(current), "20 seconds", old_parent, old_dto)
    if mode in {"invalid-json", "missing-output"}:
        with pytest.raises(L1ExtractorUnavailable):
            chat.say(SOURCE)
    else:
        chat.say(SOURCE)
    for reopened in (False, True):
        if reopened:
            chat.close()
            chat.open()
        assert chat.recall(current) == []
        earlier = chat.recall("As of 2020-04-01, what was engine-0064 sampling period?")
        assert_literal_evidence(chat, earlier, "20 seconds", old_parent, old_dto)
        assert all("35 seconds" not in item["summary"] for item in earlier)
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))
    records = chat.runtime.store.list_records(kinds=["memory"], scope=scope, source_ids=["hermes"], limit=100)
    assert not any(business_metadata(r.meta).get(KEY, {}).get("state") == "resolved"
                   and "35 seconds" in r.summary for r in records)
    assert calls == [old, SOURCE]


def test_producer_requests_only_literal_format_and_retains_identity_and_priorities():
    prompt = extract_memories_system_prompt("用户（SyntheticOperator）")
    assert '"schema":"ordinary_temporal_literals.v1"' in prompt
    assert '"slot":"原文完整事实字段短语"' in prompt
    assert '"valid_until":null' in prompt
    assert '"span":' not in prompt and '"subject":' not in prompt and '"attribute":' not in prompt
    assert "用户（SyntheticOperator）" in prompt and "__OPERATOR_SUBJECT__" not in prompt
    assert "instruction <70" in prompt and "persona <50" in prompt
    assert "episodic <60" in prompt and "fact <60" in prompt
    assert "source_message_ids 只能从【权威来源消息ID】选择" in prompt
