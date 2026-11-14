"""Focused synthetic checks; parent executes in its established containment."""
from copy import deepcopy
from hashlib import sha256

import pytest

from test_ordinary_chat_temporal_semantics import chat  # existing real-chain fixture
from eimemory.knowledge.chat_temporal import ground_assertion, query_time


def proposal(text, subject, attribute, value, start, **extra):
    def literal(part):
        at = text.index(part)
        return {"text": part, "span": [at, at + len(part)]}
    return {
        "source_digest": sha256(text.encode()).hexdigest(),
        "subject": literal(subject), "attribute": literal(attribute),
        "value": literal(value), "valid_from": literal(start),
        "valid_until": None, "operation": "claim", **extra,
    }


def test_structured_proposal_requires_exact_original_digest_and_spans():
    text = "自2021-06-01起，设备甲的区域是东区。"
    valid = proposal(text, "设备甲", "区域", "东区", "2021-06-01")
    grounded = ground_assertion(text, text, valid)
    assert grounded["state"] == "resolved"
    assert grounded["fact"]["entity_id"] == "设备甲"
    for mutate in (
        lambda p: p.update(source_digest="0" * 64),
        lambda p: p["subject"].update(text="设备乙"),
        lambda p: p["value"].update(span=[0, 2]),
        lambda p: p.update(authority="operator"),
        lambda p: p.update(targets=["arbitrary-record"]),
    ):
        bad = deepcopy(valid)
        mutate(bad)
        assert ground_assertion(text, text, bad)["state"] == "unresolved"


@pytest.mark.parametrize("text", [
    "自2021-06-01起，它的区域是东区。",
    "自2021-02-30起，node007区域是东区。",
    "自03/04/2021起，node007区域是东区。",
    "自2021-06-01起，node007区域可能是东区。",
    "自2021-06-01起，node007区域不是东区。",
])
def test_uncertain_identity_date_or_assertion_is_unresolved(text):
    assert ground_assertion(text, text)["state"] == "unresolved"


def test_opaque_identity_case_and_zeroes_are_literal():
    identities = []
    for subject in ("node001", "node1", "Node001"):
        text = f"自2021-06-01起，{subject}区域是东区。"
        identities.append(ground_assertion(text, text)["fact"])
    assert len({p["entity_id"] for p in identities}) == 3


def test_date_in_identifier_and_alias_span_do_not_authorize_an_entity():
    text = "工单编号2021-06-01，设备甲的区域是东区。"
    p = proposal(text, "设备甲", "区域", "东区", "2021-06-01")
    assert ground_assertion(text, text, p)["state"] == "unresolved"
    text = "自2021-06-01起，设备甲（别名设备乙）的区域是东区。"
    p = proposal(text, "设备乙", "区域", "东区", "2021-06-01")
    assert ground_assertion(text, text, p)["state"] == "unresolved"


def test_chinese_calendar_and_explicit_offset_are_business_dates():
    text = "自2021年6月1日起，unit004区域是东区。"
    assert ground_assertion(text, text)["interval"]["valid_from"] == "2021-06-01T00:00:00+00:00"
    text = "自2021-06-01T08:00:00+08:00起，unit004区域是东区。"
    assert ground_assertion(text, text)["interval"]["valid_from"] == "2021-06-01T00:00:00+00:00"


def test_question_dates_require_temporal_context_and_known_time_is_not_replayed():
    assert query_time("截至2021年6月1日，node007区域是什么？")[0] == "2021-06-01T00:00:00+00:00"
    assert query_time("As of 2021-06-01, node007's region?")[0] == "2021-06-01T00:00:00+00:00"
    assert query_time("查询编号2021-06-01的区域")[0] is None
    assert query_time("As of 03/04/2021, node007's region?")[1] is False
    assert query_time("node007过去的区域是什么？")[1] is False
    assert query_time("As of 2021-06-01, asset2020-01-01's region?")[0] == "2021-06-01T00:00:00+00:00"


def test_future_transition_changes_only_at_the_valid_boundary(chat):
    chat.say("自2021-06-01起，node007区域是East。")
    future = chat.say("自2099-07-01起，node007区域改为West。")
    current = chat.recall("node007现在的区域是什么？")
    assert any("East" in item["summary"] for item in current)
    assert all("West" not in item["summary"] for item in current)
    boundary = chat.recall("在2099-07-01，node007区域是什么？")
    assert any("West" in item["summary"] and item["record_id"] != future for item in boundary)
    assert all("East" not in item["summary"] for item in boundary)


def test_english_later_dated_state_creates_a_business_boundary(chat):
    chat.say("Since 2021-01-01, edge-02's rack is R1.")
    chat.say("As of 2021-02-01, edge-02's rack is R2.")
    items = chat.recall("edge-02's rack now?")
    assert any("R2" in item["summary"] for item in items)
    assert all("R1" not in item["summary"] for item in items)
    items = chat.recall("As of 2021-01-15, edge-02's rack?")
    assert any("R1" in item["summary"] for item in items)
    assert all("R2" not in item["summary"] for item in items)


def test_bounded_retrospective_correction_does_not_erase_other_dates(chat):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("更正：从2021-02-01到2021-03-01，node007区域是West。")
    for day, expected, excluded in (("2021-01-15", "East", "West"),
                                    ("2021-02-15", "West", "East"),
                                    ("2021-03-01", "East", "West")):
        items = chat.recall(f"在{day}，node007区域是什么？")
        assert any(expected in item["summary"] for item in items)
        assert all(excluded not in item["summary"] for item in items)


def test_bounded_retraction_does_not_resurrect_a_predecessor(chat):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-02-01起，node007区域改为West。")
    chat.say("撤回：从2021-02-10到2021-02-20，node007区域是West。")
    assert chat.recall("在2021-02-15，node007区域是什么？") == []
    for day in ("2021-02-05", "2021-02-20"):
        items = chat.recall(f"在{day}，node007区域是什么？")
        assert any("West" in item["summary"] for item in items)
        assert all("East" not in item["summary"] for item in items)


def test_normal_queue_and_idempotent_replay_use_same_temporal_route(chat, monkeypatch):
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-02-01起，node007区域改为West。")
    before = chat.recall("node007现在的区域是什么？")
    chat.turn_number -= 1  # replay the same original service event, not a new write
    chat.say("自2021-02-01起，node007区域改为West。")
    assert chat.recall("node007现在的区域是什么？") == before
    assert any("West" in item["summary"] for item in before)
    assert all("East" not in item["summary"] for item in before)


def test_original_source_change_fails_closed_without_all_version_fallback(chat):
    old_id = chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-02-01起，node007区域改为West。")
    from eimemory.models.records import ScopeRef
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from test_ordinary_chat_temporal_semantics import SCOPE
    scope = ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))

    def mutate(sqlite):
        original = sqlite.get_by_exact_ref(old_id, scope=scope, source_id="hermes")
        original.content["text"] += " Altered source."
        sqlite.upsert(original, commit=False)
        return None, [original], []

    chat.runtime.store.mutate_records_atomically(mutate)
    items = chat.recall("node007现在的区域是什么？")
    assert all("East" not in item["summary"] and "West" not in item["summary"] for item in items)


def test_normal_memory_api_resolves_without_adapter_or_manual_time_kwargs(chat):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-02-01起，node007区域改为West。")
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from test_ordinary_chat_temporal_semantics import SCOPE
    bundle = chat.runtime.memory.recall(
        query="在2021-01-15，node007区域是什么？",
        scope=resolve_channel_scope("hermes", SCOPE),
        task_context={"source_ids": ["hermes"]},
    )
    assert any("East" in item.summary for item in bundle.items)
    assert all("West" not in item.summary for item in bundle.items)


def test_undated_new_assertion_does_not_become_a_known_current_value(chat):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("node007区域是West。")
    items = chat.recall("node007现在的区域是什么？")
    assert any("East" in item["summary"] for item in items)
    assert all("West" not in item["summary"] for item in items)


def test_new_helper_has_exact_release_classification():
    from eimemory.governance.release.release_impact import DOMAIN_PATHS
    path = "eimemory/knowledge/chat_temporal.py"
    assert path in DOMAIN_PATHS["memory.recall"]
    assert path in DOMAIN_PATHS["memory.governance"]
    assert path in DOMAIN_PATHS["storage.integrity"]
    assert path + ".unreviewed" not in DOMAIN_PATHS["memory.recall"]


def test_structured_model_dto_delivers_original_quote_and_caller_partition(chat, monkeypatch):
    import json
    from types import SimpleNamespace
    from eimemory.adapters.runtime import service
    from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
    text = "host-008's sector is North effective 2021-06-01."
    p = proposal(text, "host-008", "sector", "North", "2021-06-01")

    class Model:
        def complete(self, **kwargs):
            return SimpleNamespace(text=json.dumps([{
                "content": "A model paraphrase with an InventedValue.",
                "type": "fact", "priority": 90, "temporal": p,
            }]))

    def extract(*args, **kwargs):
        kwargs.update(llm=Model(), use_llm=True, fallback_heuristic=False)
        return extract_l1_from_l0_record(*args, **kwargs)

    monkeypatch.setattr(service, "extract_l1_from_l0_record", extract)
    parent = chat.say(text)
    items = chat.recall("host-008's sector now?")
    assert any("North" in item["summary"] and item["record_id"] != parent for item in items)
    assert all("InventedValue" not in item["summary"] for item in items)
    assert all(item["source_id"] == "hermes" for item in items)


def test_known_time_replay_is_explicitly_unsupported_on_normal_api(chat):
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from test_ordinary_chat_temporal_semantics import SCOPE
    with pytest.raises(ValueError, match="known_at_unsupported"):
        chat.runtime.memory.recall(query="What did you know as of 2021-06-01?",
                                   scope=resolve_channel_scope("hermes", SCOPE))


def test_chat_temporal_resolution_keeps_exact_source_and_owner_partition(chat):
    from test_ordinary_chat_temporal_semantics import SCOPE
    chat.say("自2021-01-01起，node007区域是East。")
    for channel, scope, value in (("codex", SCOPE, "ForeignSource"),
                                   ("hermes", {**SCOPE, "user_id": "different-owner"}, "OtherOwner")):
        result = chat.service.sync_turn(channel=channel, scope=scope, session_id="other-chat",
            turn_id="1", user_text=f"自2021-01-01起，node007区域是{value}。", assistant_text="Acknowledged.")
        assert result["ok"] is True
        if result.get("l1_queued"):
            assert chat.service.drain_l1_queue(limit=1) == 1
    items = chat.recall("node007现在的区域是什么？")
    assert any("East" in item["summary"] for item in items)
    assert all("ForeignSource" not in item["summary"] and "OtherOwner" not in item["summary"] for item in items)


def test_interrupted_journal_append_is_hidden_and_exact_retry_completes(chat, monkeypatch):
    original = chat.runtime.store.apply_temporal_facts
    calls = []

    def interrupted(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic interruption before journal commit")
        return original(**kwargs)

    monkeypatch.setattr(chat.runtime.store, "apply_temporal_facts", interrupted)
    text = "自2021-01-01起，node007区域是East。"
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        chat.say(text)
    assert chat.recall("node007现在的区域是什么？") == []
    chat.turn_number -= 1
    chat.say(text)
    items = chat.recall("node007现在的区域是什么？")
    assert len(items) == 1 and "East" in items[0]["summary"]


def test_incomplete_family_cannot_fall_back_to_all_legacy_versions(chat, monkeypatch):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-02-01起，node007区域改为West。")
    monkeypatch.setattr(type(chat.runtime.store.sqlite), "temporal_fact_family", lambda *a, **k: ([], False))
    assert chat.recall("node007现在的区域是什么？") == []


def test_attribute_prefix_is_not_the_same_field(chat):
    chat.say("自2021-01-01起，node007区域是East。")
    chat.say("自2021-01-01起，node007区域编码是E01。")
    items = chat.recall("node007现在的区域编码是什么？")
    assert any("E01" in item["summary"] for item in items)
    assert all("East" not in item["summary"] for item in items)
