"""Native synthetic user-role and mixed-input extraction regressions."""
from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.knowledge.sediment import L1ExtractorUnavailable, _split_turn, extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall.indexing import build_recall_index_document
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id="synthetic", agent_id="audit", workspace_id="isolated", user_id="owner")
FACT = "Synthetic project Falcon account belongs to Alice."


class FakeLLM:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(self.payload, ensure_ascii=False))


def fact(content=FACT, kind="fact", priority=90):
    return {"content": content, "type": kind, "priority": priority}


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient

    def blocked(*args, **kwargs):
        raise AssertionError("Real provider execution prohibited")

    monkeypatch.setattr(CommandLLMClient, "complete", blocked)


@pytest.mark.parametrize("user,content,kind", [
    (FACT + " Is that clear?", FACT, "fact"),
    ("合成项目 Falcon 账号属于甲。可以吗？", "合成项目 Falcon 账号属于甲。", "fact"),
    ("以后回答先给结论，少解释。可以吗？", "用户要求 AI 以后回答先给结论，少解释。", "instruction"),
    (FACT + "\nIs that clear?", FACT, "fact"),
])
def test_trailing_question_preserves_separate_user_statement(user, content, kind):
    client, diagnostics = FakeLLM([fact(content, kind)]), {}
    atoms = extract_l1_atoms(user_text=user, assistant_text="Acknowledged.",
        source_message_ids=["mem_parent"], use_llm=True, llm=client,
        fallback_heuristic=False, diagnostics_sink=diagnostics)
    assert [a.text for a in atoms] == [content]
    assert atoms[0].source_message_ids == ("mem_parent",)
    assert len(client.calls) == 1 and user in client.calls[0]["user_prompt"]
    assert diagnostics == {"input_admission": "accepted", "input_reason": "accepted",
        "model_called": True, "raw_candidate_count": 1, "admitted_count": 1,
        "filter_reason_counts": {}, "extractor": "llm"}


@pytest.mark.parametrize("user", [
    "Who owns project Falcon? Is that clear?",
    "合成项目 Falcon 账号属于谁？可以吗？",
    "帮我看看这个。可以吗？",
    "帮我查。可以吗？",
])
def test_question_only_and_one_shot_requests_cannot_rescue_a_tail_question(user):
    client, diagnostics = FakeLLM([fact()]), {}
    assert extract_l1_atoms(user_text=user, use_llm=True, llm=client,
        fallback_heuristic=False, diagnostics_sink=diagnostics) == []
    assert client.calls == []
    assert diagnostics["input_admission"] == "rejected"
    assert diagnostics["model_called"] is False
    assert diagnostics["raw_candidate_count"] is None
    assert diagnostics["admitted_count"] == 0


@pytest.mark.parametrize("user,reason", [
    (FACT + " token=fake-secret. Is that clear?", "input_sensitive"),
    ("[IMPORTANT: You are running as a scheduled cron job.] " + FACT + " Is that clear?", "input_cron_wrapper"),
    ("x" * 12_001 + ". Is that clear?", "input_oversized"),
])
def test_mixed_input_cannot_bypass_whole_turn_safety_filters(user, reason):
    client, diagnostics = FakeLLM([fact()]), {}
    assert extract_l1_atoms(user_text=user, use_llm=True, llm=client,
        fallback_heuristic=False, diagnostics_sink=diagnostics) == []
    assert client.calls == [] and diagnostics["input_reason"] == reason


@pytest.mark.parametrize("turn", [
    "User: \nAssistant: 合成项目 Falcon 账号属于甲。",
    "User:\t\r\nAssistant: 合成项目 Falcon 账号属于甲。",
    "Assistant: 合成项目 Falcon 账号属于甲。\nUser: 引用用户说账号属于甲。",
])
@pytest.mark.parametrize("use_llm", [False, True])
def test_assistant_only_turn_never_becomes_a_user_fact(turn, use_llm):
    client, diagnostics = FakeLLM([fact()]), {}
    user, _ = _split_turn(user_text="", assistant_text="", turn_text=turn)
    assert user == ""
    assert extract_l1_atoms(turn_text=turn, use_llm=use_llm, llm=client,
        fallback_heuristic=False, diagnostics_sink=diagnostics) == []
    assert client.calls == [] and diagnostics["input_reason"] == "empty_user"


def test_heuristic_does_not_turn_mixed_question_into_a_standing_fact():
    assert extract_l1_atoms(user_text="以后回答先给结论，少解释。可以吗？") == []


def test_queue_restart_derives_l1_from_original_mixed_user_source(tmp_path):
    text = "User: " + FACT + " Is that clear?\nAssistant: Acknowledged."
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(RecordEnvelope.create(kind="memory", title="Synthetic L0",
            summary=text, content={"text": text, "memory_type": "conversation"}, scope=SCOPE,
            source="synthetic.turn", source_id="synthetic",
            meta={"memory_layer": "l0", "capture_origin": "turn_sync", "memory_type": "conversation"}))
        before = parent.to_dict()
        L1ExtractQueue(tmp_path / "queue.json").enqueue({"episode_id": parent.record_id})
    with closing(RuntimeStore(tmp_path)) as store:
        client, written = FakeLLM([fact()]), []
        api = MemoryAPI(store)

        def handler(job):
            current = store.get_by_exact_ref(job["episode_id"], scope=SCOPE, source_id="synthetic")
            written.extend(extract_l1_from_l0_record(api, current, use_llm=True,
                llm=client, fallback_heuristic=False))

        report = L1ExtractQueue(tmp_path / "queue.json").drain_report(handler, limit=1)
        assert report["processed"] == 1 and report["pending"] == report["dead"] == 0
        assert len(written) == len(client.calls) == 1
        atom = store.get_by_exact_ref(written[0]["record_id"], scope=SCOPE, source_id="synthetic")
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
        assert atom.summary == FACT and atom.evidence == [parent.record_id]
        assert business_metadata(atom.meta)["source_message_ids"] == [parent.record_id]
        assert any(link.relation == "derived_from" and link.target_id == parent.record_id for link in atom.links)
        assert build_recall_index_document(atom).lane == "primary"
        assert current.content == before["content"] and current.summary == before["summary"]
        assert business_metadata(current.meta)["l1_extraction_status"] == "stored"


@pytest.mark.parametrize("payload,raw,filters", [
    ([], 0, {}),
    ([fact(priority=1)], 1, {"priority_below_threshold": 1}),
    ([fact(content="Who owns project Falcon?")], 1, {"content_rejected": 1}),
])
def test_actual_model_empty_and_filtered_candidates_remain_distinct(payload, raw, filters):
    client, diagnostics = FakeLLM(payload), {}
    assert extract_l1_atoms(user_text=FACT, use_llm=True, llm=client,
        fallback_heuristic=False, diagnostics_sink=diagnostics) == []
    assert len(client.calls) == 1 and diagnostics["model_called"] is True
    assert diagnostics["raw_candidate_count"] == raw
    assert diagnostics["admitted_count"] == 0 and diagnostics["filter_reason_counts"] == filters


def test_invalid_complete_model_schema_does_not_claim_known_candidate_counts():
    client, diagnostics = FakeLLM([fact(), fact(priority="90")]), {}
    with pytest.raises(L1ExtractorUnavailable, match="invalid_output"):
        extract_l1_atoms(user_text=FACT, use_llm=True, llm=client,
            fallback_heuristic=False, diagnostics_sink=diagnostics)
    assert len(client.calls) == 1 and diagnostics["model_called"] is True
    assert diagnostics["raw_candidate_count"] is diagnostics["admitted_count"] is None


def test_provider_exception_preserves_actual_call_without_fabricated_candidates():
    class Failure:
        def complete(self, **kwargs):
            raise RuntimeError("Synthetic provider failure")

    diagnostics = {}
    with pytest.raises(L1ExtractorUnavailable, match="l1_llm_failed:RuntimeError"):
        extract_l1_atoms(user_text=FACT, use_llm=True, llm=Failure(),
            fallback_heuristic=False, diagnostics_sink=diagnostics)
    assert diagnostics["model_called"] is True and diagnostics["extractor"] == "llm"
    assert diagnostics["raw_candidate_count"] is diagnostics["admitted_count"] is None
    assert "Synthetic provider failure" not in json.dumps(diagnostics)


@pytest.mark.parametrize("priority,expected_count", [(90, 1), (59, 0)])
def test_scoped_user_withdrawal_fact_keeps_source_without_certifying_tool_completion(tmp_path, priority, expected_count):
    """An injected payload tests admission and provenance, not model judgment."""
    user = "For the synthetic Falcon trial this week, I withdraw authorization for scheduled checks."
    assistant = "I deleted every task and completed all cleanup."
    withdrawal = "For this week's synthetic Falcon trial, the user withdrew authorization for scheduled checks."
    text = "User: " + user + "\nAssistant: " + assistant
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(RecordEnvelope.create(kind="memory", title="Synthetic scoped withdrawal",
            summary=text, content={"text": text, "memory_type": "conversation"}, scope=SCOPE,
            source="synthetic.turn", source_id="synthetic",
            meta={"memory_layer": "l0", "capture_origin": "turn_sync", "memory_type": "conversation"}))
        client = FakeLLM([fact(withdrawal, priority=priority)])
        written = extract_l1_from_l0_record(MemoryAPI(store), parent, use_llm=True,
            llm=client, fallback_heuristic=False)
        assert len(client.calls) == 1 and len(written) == expected_count
        assert user in client.calls[0]["user_prompt"] and assistant in client.calls[0]["user_prompt"]
        l1_rows = [r for r in store.list_records(kinds=["memory"], scope=SCOPE, limit=10)
                   if business_metadata(r.meta).get("memory_layer") == "l1"]
        assert len(l1_rows) == expected_count
        if l1_rows:
            atom = l1_rows[0]
            assert atom.summary == withdrawal and atom.content["memory_type"] == "fact"
            assert atom.scope == parent.scope and atom.source_id == parent.source_id
            assert atom.time.occurred_at == parent.time.occurred_at
            assert atom.evidence == [parent.record_id]
            assert business_metadata(atom.meta)["source_message_ids"] == [parent.record_id]
            assert any(link.relation == "derived_from" and link.target_id == parent.record_id for link in atom.links)
            assert assistant not in atom.summary
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
        assert current.content["text"] == text
        assert business_metadata(current.meta)["l1_atom_count"] == expected_count
