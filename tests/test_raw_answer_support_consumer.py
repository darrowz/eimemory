"""Real consumer integration with a stub transport, not semantic accuracy tests."""
from dataclasses import asdict
import json
from time import perf_counter, sleep

import pytest

from eimemory.raw.retrieval import search_raw_chunks
from eimemory.recall.raw_citation import attach_compact_lane, collect_citation, render_citation
from eimemory.retrieval import caller_assistance
from test_raw_answer_support import Client, ROUTE, no_support
from test_runtime_raw_citation_delivery import (
    CANONICAL, QUERY, TEXT, add_raw, call, configure_proactive, delivery_expectation,
    final_context, owner, persona, persist_mutation, proactive, runtime,
)


def configured(monkeypatch, client):
    monkeypatch.setenv("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", "1")
    monkeypatch.delenv("EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED", raising=False)
    monkeypatch.delenv("EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED", raising=False)
    monkeypatch.setattr(caller_assistance, "route_for_request", lambda channel, *, scope: dict(ROUTE))
    monkeypatch.setattr(caller_assistance, "configured_client", lambda: client)


def fetch(runtime, query=QUERY, **context):
    return call(runtime, base=CANONICAL, query=query, task_context={"source_ids": ["hermes"], **context})


def test_disabled_feature_is_unknown_unreviewed_and_caller_cannot_enable_it(runtime, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", raising=False)
    monkeypatch.setattr(caller_assistance, "configured_client", lambda: pytest.fail("disabled model call"))
    raw = add_raw(runtime, scope=owner(CANONICAL))
    result = fetch(runtime, answer_support={"status": "supported"}, enable_answer_support=True)
    lane = result["bundle"]["raw_citation"]
    assert lane["answer_support"] == {"status":"unknown", "reason":"feature_disabled", "reviewed":False,
                                      "scope":"presented_candidate_set", "calls":0, "selected":[]}
    assert lane["evidence"][0]["ref"][0] == raw.record_id
    assert "entailment is not established" in result["context"]


def test_supported_only_promotes_selected_original_source_and_keeps_related(runtime, monkeypatch):
    client = Client()
    configured(monkeypatch, client)
    first = add_raw(runtime, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests require an internal review.", scope=owner(CANONICAL))
    result = fetch(runtime)
    lane = result["bundle"]["raw_citation"]
    assert lane["decision"]["status"] == lane["admission"]["status"] == "related"
    assert lane["answer_support"]["status"] == "supported" and lane["answer_support"]["reviewed"]
    assert len(lane["evidence"]) == 2
    assert len(lane["support_evidence"]) == 1
    proof = lane["answer_support"]["selected"][0]
    assert lane["support_evidence"][0]["ref"] == list(proof["ref"])
    assert lane["support_evidence"][0]["text"] == json.loads(client.calls[0]["user_prompt"])["passages"][0]["text"]
    assert "selected by a model" in result["context"] and "not a proof of truth" in result["context"]
    assert "does not inherit that support judgment" in result["context"]
    assert '"evidence_role":"related_material"' in result["context"]
    assert '"evidence_role":"support_material"' in result["context"]


def test_model_selection_does_not_rerank_original_candidates(runtime, monkeypatch):
    client = Client(lambda packet: {"request_digest":packet["request_digest"], "status":"supported",
        "reason":"direct_support", "selected":[p["id"] for p in reversed(packet["passages"])]})
    configured(monkeypatch, client)
    add_raw(runtime, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests are stored in a second repository.", scope=owner(CANONICAL))
    result = fetch(runtime)
    lane = result["bundle"]["raw_citation"]
    texts = [row["text"] for row in lane["support_evidence"]]
    presented = [row["text"] for row in json.loads(client.calls[0]["user_prompt"])["passages"]]
    assert texts == presented
    assert [row["text"] for row in lane["evidence"]] == presented


def test_unsupported_withholds_fact_quotes_but_ordinary_raw_still_returns_related(runtime, monkeypatch):
    client = Client(no_support)
    configured(monkeypatch, client)
    raw = add_raw(runtime, scope=owner(CANONICAL))
    result = fetch(runtime)
    lane = result["bundle"]["raw_citation"]
    assert lane["answer_support"]["status"] == "unsupported" and not lane["support_evidence"]
    assert lane["evidence"][0]["ref"][0] == raw.record_id
    assert lane["answer_support"]["scope"] == "presented_candidate_set"
    assert "not a claim that no answer exists elsewhere" in result["context"]
    assert "must not be presented as a supported answer" in result["context"]
    assert '"evidence_role":"support_material"' not in result["context"]
    ordinary = search_raw_chunks(runtime.store, query=QUERY, scope=owner(CANONICAL), limit=5,
        source_ids=["hermes"], task_context={"enable_llm_rerank":False, "rerank_with_llm":False})
    assert any(row["record"]["record_id"] == raw.record_id for row in ordinary)


@pytest.mark.parametrize("failure", ["timeout", "bad_identity", "bad_binding", "unconfigured"])
def test_unknown_keeps_still_current_related_material_without_claiming_support(runtime, monkeypatch, failure):
    class TimedOut(Client):
        def complete(self, **kwargs):
            raise TimeoutError
    client = (TimedOut() if failure == "timeout" else Client(model="unexpected") if failure == "bad_identity"
              else Client(lambda packet: {"request_digest":"0"*64,"status":"supported",
                   "reason":"direct_support","selected":["p0w0"]}) if failure == "bad_binding" else None)
    configured(monkeypatch, client)
    raw = add_raw(runtime, scope=owner(CANONICAL))
    result = fetch(runtime)
    lane = result["bundle"]["raw_citation"]
    assert lane["answer_support"]["status"] == "unknown" and not lane["answer_support"]["reviewed"]
    assert lane["evidence"][0]["ref"][0] == raw.record_id
    assert not lane["support_evidence"]
    assert "Answer support is unknown" in result["context"]


@pytest.mark.parametrize("mutation", ["withdrawn", "text", "new_candidate"])
def test_post_model_source_snapshot_race_cannot_deliver_stale_verdict(runtime, monkeypatch, mutation):
    raw = add_raw(runtime, scope=owner(CANONICAL))
    persona(runtime, CANONICAL)
    def response(packet):
        if mutation == "new_candidate":
            add_raw(runtime, "Halcyon deployment manifests are stored in a newly published repository.", scope=owner(CANONICAL))
        else:
            if mutation == "withdrawn":
                raw.status = "withdrawn"
            else:
                raw.content["raw_text"] = raw.content["text"] = "Halcyon deployment manifests moved elsewhere."
            persist_mutation(runtime, raw)
        return {"request_digest":packet["request_digest"],"status":"supported",
                "reason":"direct_support","selected":["p0w0"]}
    configured(monkeypatch, Client(response))
    result = fetch(runtime)
    lane = result["bundle"]["raw_citation"]
    assert lane["answer_support"]["status"] == "unknown" and not lane["evidence"]
    assert not lane["support_evidence"]
    assert lane["admission"]["reason"] == "support_snapshot_changed"
    assert "infrastructure repository" not in result["context"]
    assert "Prefer concise" in result["context"]


def test_hard_delivery_deadline_is_not_extended_for_a_slow_model(runtime, monkeypatch):
    class Late(Client):
        def complete(self, **kwargs):
            sleep(0.04)
            return super().complete(**kwargs)
    configured(monkeypatch, Late())
    add_raw(runtime, scope=owner(CANONICAL))
    with caller_assistance.host_delivery_deadline(perf_counter() + 0.02):
        lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL),
            context={"runtime_channel":"hermes", "source_ids":["hermes"]})
    assert lane.answer_support["status"] == "unknown"
    assert not lane.evidence
    assert not lane.support_evidence


def test_compact_or_prompt_truncation_cannot_keep_whole_query_support(runtime, monkeypatch):
    configured(monkeypatch, Client(lambda packet: {"request_digest":packet["request_digest"],
        "status":"supported", "reason":"direct_support", "selected":[p["id"] for p in packet["passages"]]}))
    add_raw(runtime, TEXT + " " + "More source detail. " * 20, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests are stored in the secondary repository.", scope=owner(CANONICAL))
    lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL),
        context={"runtime_channel":"hermes", "source_ids":["hermes"]})
    assert lane.answer_support["status"] == "supported"
    public = lane.to_dict()
    rendered = render_citation("", public, max_chars=750)
    assert public["answer_support"]["status"] == "unknown"
    assert "selected by a model" not in rendered
    compact = attach_compact_lane({}, lane, maximum_bytes=2200)
    if "raw_citation" in compact:
        assert compact["raw_citation"]["answer_support"]["status"] == "unknown"


def test_cached_negative_review_is_invalidated_when_the_candidate_snapshot_changes(runtime, monkeypatch):
    configured(monkeypatch, Client(no_support))
    add_raw(runtime, scope=owner(CANONICAL))
    persona(runtime, CANONICAL)
    configure_proactive(runtime)
    payload = proactive(runtime, base=CANONICAL)
    assert payload["bundle"]["raw_citation"]["answer_support"]["status"] == "unsupported"
    assert "reviewed candidate set" in final_context(runtime, payload, base=CANONICAL)
    add_raw(runtime, "Halcyon deployment manifests are stored in a newly published repository.", scope=owner(CANONICAL))
    expected = delivery_expectation(runtime, base=CANONICAL)
    delivered = final_context(runtime, payload, base=CANONICAL, expected_authority=expected)
    assert "reviewed candidate set" not in delivered and "Prefer concise" in delivered
