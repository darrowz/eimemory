"""Seen bounded-operation regressions; parent executes under OS containment.

Only synthetic model output is replaced. These tests exercise ordinary capture,
source certification, recall and reopening; writing them is not an execution.
"""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from test_chat_temporal_reviewfix1 import assert_exact_evidence
from test_ordinary_chat_temporal_semantics import SourceEchoModel, chat
from eimemory.contracts.temporal_facts import parse_instant
from eimemory.knowledge.chat_temporal import _source_contradicts, ground_assertion


OLD = "自2021-01-01起，river021区域是East。"
OPERATIONS = [("retraction", "撤回", "East"), ("correction", "更正", "West")]


def replace_output(monkeypatch, user, operation, value, mode):
    original = SourceEchoModel.complete

    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            return original(self, **kwargs)
        actual = prompt.split("\nUSER:\n", 1)[1].split("\n\nASSISTANT:\n", 1)[0]
        if actual != user:
            return original(self, **kwargs)
        if mode == "omitted":
            return SimpleNamespace(text="[]")
        proposal = {
            "schema": "ordinary_temporal_literals.v1",
            "source_digest": sha256(user.encode()).hexdigest(),
            "slot": "river021区域", "value": value,
            "valid_from": "2021-02-01", "valid_until": "2023-01-01",
            "operation": operation,
        }
        if mode == "bad-digest":
            proposal["source_digest"] = "0" * 64
        elif mode == "wrong-operation":
            proposal["operation"] = "claim"
        elif mode == "invalid-interval":
            proposal["valid_until"] = "2020-01-01"
        else:
            raise AssertionError("Unexpected synthetic output mode")
        source_ids = json.loads(prompt.split("【权威来源消息ID】", 1)[1].split("\n", 1)[0])
        return SimpleNamespace(text=json.dumps([{
            "content": user, "type": "fact", "priority": 90,
            "source_message_ids": source_ids, "temporal": proposal,
        }], ensure_ascii=False))

    monkeypatch.setattr(SourceEchoModel, "complete", complete)


@pytest.mark.parametrize("operation,marker,value", OPERATIONS, ids=["retraction", "correction"])
@pytest.mark.parametrize("mode", ["omitted", "bad-digest", "wrong-operation", "invalid-interval"])
def test_unrepresented_bounded_operation_subtracts_only_its_interval(chat, monkeypatch, operation, marker, value, mode):
    old_parent = chat.say(OLD)
    changed = f"{marker}：从2021-02-01到2023-01-01，river021区域是{value}。"
    replace_output(monkeypatch, changed, operation, value, mode)
    chat.say(changed)
    for date in ("2021-02-01", "2022-01-01"):
        assert chat.recall(f"在{date}，river021区域是什么？") == []
    for date in ("2021-01-15", "2023-01-01", "2024-01-01"):
        assert_exact_evidence(chat, chat.recall(f"在{date}，river021区域是什么？"), "East", old_parent)
    chat.close()
    chat.open()
    assert chat.recall("在2022-01-01，river021区域是什么？") == []
    assert_exact_evidence(chat, chat.recall("在2024-01-01，river021区域是什么？"), "East", old_parent)


@pytest.mark.parametrize("operation,marker,value", OPERATIONS, ids=["retraction", "correction"])
def test_certified_bounded_operation_keeps_legitimate_interval_answers(chat, operation, marker, value):
    old_parent = chat.say(OLD)
    changed_parent = chat.say(f"{marker}：从2021-02-01到2023-01-01，river021区域是{value}。")
    inside = chat.recall("在2022-01-01，river021区域是什么？")
    if operation == "retraction":
        assert inside == []
    else:
        assert_exact_evidence(chat, inside, "West", changed_parent)
        assert all("East" not in item["summary"] for item in inside)
    for date in ("2021-01-15", "2023-01-01"):
        assert_exact_evidence(chat, chat.recall(f"在{date}，river021区域是什么？"), "East", old_parent)


@pytest.mark.parametrize("text", [
    "撤回：从2021-02-01到2023-01-01，river021区域是West。",
    "更正：从2021-02-01到2023-01-01，river021区域是East。",
    "撤回：从2021-02-01到2023-01-01，river022区域是East。",
    "撤回：从2021-02-01到2023-01-01，river021颜色是East。",
    "撤回：自2021-02-01起，river021区域是East。",
], ids=["other-value", "corroborating-correction", "other-owner", "other-attribute", "unbounded-unsupported"])
def test_omitted_noncontradicting_operation_preserves_selected_evidence(chat, monkeypatch, text):
    old_parent = chat.say(OLD)
    replace_output(monkeypatch, text, "retraction", "East", "omitted")
    chat.say(text)
    assert_exact_evidence(chat, chat.recall("在2022-01-01，river021区域是什么？"), "East", old_parent)


@pytest.mark.parametrize("operation,marker,value", OPERATIONS, ids=["retraction", "correction"])
def test_omitted_local_operation_can_cover_a_later_starting_claim(chat, monkeypatch, operation, marker, value):
    old_parent = chat.say(OLD)
    changed = f"{marker}：从2020-01-01到2023-01-01，river021区域是{value}。"
    replace_output(monkeypatch, changed, operation, value, "omitted")
    chat.say(changed)
    assert chat.recall("在2022-01-01，river021区域是什么？") == []
    assert_exact_evidence(chat, chat.recall("在2023-01-01，river021区域是什么？"), "East", old_parent)


@pytest.mark.parametrize("operation,marker,value", OPERATIONS, ids=["retraction", "correction"])
def test_future_unrepresented_bounded_operation_does_not_erase_current(chat, monkeypatch, operation, marker, value):
    old_parent = chat.say(OLD)
    changed = f"{marker}：从2098-01-01到2099-01-01，river021区域是{value}。"
    replace_output(monkeypatch, changed, operation, value, "omitted")
    chat.say(changed)
    assert_exact_evidence(chat, chat.recall("在2024-01-01，river021区域是什么？"), "East", old_parent)
    assert chat.recall("在2098-06-01，river021区域是什么？") == []
    assert_exact_evidence(chat, chat.recall("在2099-01-01，river021区域是什么？"), "East", old_parent)


@pytest.mark.parametrize("operation,marker,value", OPERATIONS, ids=["retraction", "correction"])
def test_certified_exemption_requires_exact_parent_digest_and_clause(operation, marker, value):
    selected = ground_assertion(OLD, OLD)
    text = f"{marker}：从2021-02-01到2023-01-01，river021区域是{value}。"
    original = ground_assertion(text, text)
    assert original["state"] == "resolved"
    full_user = text + "另一个独立句子。"
    full_digest = sha256(full_user.encode()).hexdigest()
    originals = [("synthetic-parent", full_digest, original)]
    valid_at = parse_instant("2022-01-01T00:00:00+00:00")
    assert _source_contradicts(selected, originals, valid_at, set())
    exact = ("synthetic-parent", full_digest, original["quote"])
    assert not _source_contradicts(selected, originals, valid_at, {exact})
    for certified in (
        {("other-parent", full_digest, original["quote"])},
        {("synthetic-parent", original["user_digest"], original["quote"])},
        {("synthetic-parent", full_digest, OLD)},
    ):
        assert _source_contradicts(selected, originals, valid_at, certified)
