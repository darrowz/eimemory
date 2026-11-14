"""Ordinary-chat semantic contract; execute only in the parent's OS containment.

The fake model echoes source assertions and never chooses a winning value,
date, identity, or supersession target. Ingest, extraction, persistence, recall,
and evidence binding remain production paths. No temporal API is called.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eimemory.adapters.runtime import service as service_module
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.llm.command_client import CommandLLMClient
from eimemory.models.records import ScopeRef
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


# Synthetic identity supplied by the trusted service caller, never the model.
SCOPE = {
    "tenant_id": "synthetic",
    "agent_id": "temporal-chat-test",
    "workspace_id": "isolated",
    "user_id": "synthetic-owner",
}
OLD = "自2020-01-01起，Orchid账户负责人是Alice。"
NEW = "自2020-02-01起，Orchid账户负责人改为Bob。"
OTHER_ENTITY = "自2020-01-01起，Cedar账户负责人是Carol。"
OTHER_ATTRIBUTE = "自2020-01-01起，Orchid账户结算币种是USD。"


class SourceEchoModel:
    """Existing L1 model DTO, with no query or expected answer as input."""

    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            body = json.loads(prompt)
            # Preserve observations. Temporal authority must not come from a
            # model-selected record ID or an arbitrary legacy update action.
            payload = [
                {"record_id": item["record_id"], "action": "store", "target_ids": []}
                for item in body["new_memories"]
            ]
        else:
            user = prompt.split("\nUSER:\n", 1)[1].split("\n\nASSISTANT:\n", 1)[0]
            payload = [{"content": user, "type": "fact", "priority": 90}]
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))


class ChatHarness:
    def __init__(self, root):
        self.root = root
        self.turn_number = 0
        self.runtime = None
        self.open()

    def open(self):
        # Explicit local source avoids environment-driven provider discovery.
        # Core retains the real MemoryAPI/GovernedRecallEngine and store.
        store = RuntimeStore(self.root)
        self.runtime = Runtime(
            store, candidate_source=SQLiteCandidateSource(store), profile="core"
        )
        self.service = AgentRuntimeMemoryService(self.runtime)

    def close(self):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    def say(self, text):
        self.turn_number += 1
        result = self.service.sync_turn(
            channel="hermes", scope=SCOPE, session_id="synthetic-chat",
            turn_id=str(self.turn_number), user_text=text,
            assistant_text="Acknowledged.",
        )
        assert result["ok"] is True
        if result.get("l1_queued"):
            assert self.service.drain_l1_queue(limit=1) == 1
        parent = self.runtime.store.get_by_exact_ref(
            result["record"]["record_id"],
            scope=ScopeRef.from_dict(result["record"]["scope"]), source_id="hermes",
        )
        assert parent is not None
        assert text in parent.content["text"], "fixture must capture the original assertion"
        return parent.record_id

    def recall(self, question):
        result = self.service.prefetch(
            channel="hermes", scope=SCOPE, query=question, limit=8,
            task_context={"source_ids": ["hermes"]},
        )
        assert result["ok"] is True
        return result["bundle"]["items"]


@pytest.fixture
def chat(tmp_path, monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("External model execution is forbidden in this semantic slice")

    monkeypatch.setattr(CommandLLMClient, "complete", prohibited)
    original = service_module.extract_l1_from_l0_record
    model = SourceEchoModel()

    def deterministic_extraction(*args, **kwargs):
        # Replace only the configured model boundary; retain the real parser,
        # exact L0 binding, adjudication, source-order and completion guards.
        kwargs.update(llm=model, use_llm=True, fallback_heuristic=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "extract_l1_from_l0_record", deterministic_extraction)
    harness = ChatHarness(tmp_path / "ordinary-chat")
    try:
        yield harness
    finally:
        harness.close()


def assert_evidence(items, *, value, source_id, excluded=()):
    # Test delivered evidence, not a metadata flag or a helper invocation.
    selected = [item for item in items if value in item.get("summary", "")]
    assert selected, f"missing value {value!r} in ordinary recall evidence: {items!r}"
    assert any(item["record_id"] != source_id for item in selected), (
        "the answer must include derived durable evidence, not only an L0 transcript"
    )
    visible = "\n".join(item.get("summary", "") for item in items)
    for obsolete in excluded:
        assert obsolete not in visible, f"inapplicable value {obsolete!r} delivered: {items!r}"


def seed_transition(chat):
    old_source = chat.say(OLD)
    new_source = chat.say(NEW)
    return old_source, new_source


def test_current_fact_after_explicit_transition(chat):
    _, new_source = seed_transition(chat)
    assert_evidence(
        chat.recall("Orchid账户现在的负责人是谁？"),
        value="Bob", source_id=new_source, excluded=("Alice",),
    )


def test_historical_valid_date_after_update(chat):
    old_source, _ = seed_transition(chat)
    assert_evidence(
        chat.recall("在2020-01-15，Orchid账户负责人是谁？"),
        value="Alice", source_id=old_source, excluded=("Bob",),
    )


def test_other_entities_and_attributes_survive_transition(chat):
    cedar_source = chat.say(OTHER_ENTITY)
    currency_source = chat.say(OTHER_ATTRIBUTE)
    _, new_source = seed_transition(chat)
    # Each family must survive; then check resolution within the changed one.
    assert_evidence(chat.recall("Cedar账户现在的负责人是谁？"),
                    value="Carol", source_id=cedar_source, excluded=("Alice", "Bob"))
    assert_evidence(chat.recall("Orchid账户现在的结算币种是什么？"),
                    value="USD", source_id=currency_source, excluded=("Alice", "Bob"))
    assert_evidence(chat.recall("Orchid账户现在的负责人是谁？"),
                    value="Bob", source_id=new_source, excluded=("Alice", "Carol", "USD"))


@pytest.mark.parametrize("uncertain", [
    "Orchid账户负责人曾改为Dana，但生效日期不详。",
    "Orchid account owner changed to Dana effective 03/04/2020; date format is unspecified.",
], ids=["unknown-date", "ambiguous-date"])
def test_uncertain_business_date_cannot_replace_known_current_fact(chat, uncertain):
    _, new_source = seed_transition(chat)
    chat.say(uncertain)
    assert_evidence(
        chat.recall("Orchid账户现在的负责人是谁？"),
        value="Bob", source_id=new_source, excluded=("Alice", "Dana"),
    )


def test_reopen_preserves_current_and_historical_evidence(chat):
    old_source, new_source = seed_transition(chat)
    chat.close()
    chat.open()
    assert_evidence(chat.recall("Orchid账户现在的负责人是谁？"),
                    value="Bob", source_id=new_source, excluded=("Alice",))
    assert_evidence(chat.recall("在2020-01-15，Orchid账户负责人是谁？"),
                    value="Alice", source_id=old_source, excluded=("Bob",))
