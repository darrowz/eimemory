"""Independent recovery races with real locks, storage and monotonic deadlines."""
import threading
import json
from copy import deepcopy
from datetime import timedelta
from time import perf_counter, sleep

import pytest

from eimemory.contracts.recall_boundary import exact_ref
from eimemory.raw import support_candidates
from eimemory.raw.answer_support import MAX_PARENT_CHARS, MAX_TOTAL_CHARS
from eimemory.raw.boundary import RawRecallUnavailable, guarded_citation_search
from eimemory.raw.retrieval import search_raw_chunks
from eimemory.recall.raw_citation import collect_citation, render_citation
from eimemory.recall import raw_citation
from eimemory.retrieval import caller_assistance
from eimemory.retrieval.verification_budget import verification_budget_scope
from test_raw_answer_support import Client, no_support
from test_raw_answer_support_consumer import configured
from test_raw_support_candidate_recovery import QUERY, RELATED, MISSING, setup
from test_raw_applicability_replay import EXPIRES, wall_clock
from test_runtime_raw_citation_delivery import (
    CANONICAL, add_raw, configure_proactive, delivery_expectation, final_context, owner,
    persona, persist_mutation, proactive, runtime,
)


CONTEXT = {"source_ids": ["hermes"], "runtime_channel": "hermes", "exact_scope_only": True,
           "rerank_with_llm": False, "enable_llm_rerank": False}


def test_recovery_exact_hydration_wait_obeys_original_deadline(runtime, monkeypatch):
    first, second = setup(runtime)
    gate = guarded_citation_search(runtime.store, query=QUERY, scope=owner(CANONICAL),
        source_ids=["hermes"], task_context={**CONTEXT, "evidence_intent": "fact_citation"}, limit=5)
    original_rank = support_candidates._rank
    entered = threading.Event()
    release = threading.Event()
    workers = []

    def locked_rank(*args):
        ranked = original_rank(*args)
        def hold():
            with runtime.store._lock:
                entered.set()
                release.wait(2)
        worker = threading.Thread(target=hold)
        workers.append(worker)
        worker.start()
        assert entered.wait(1)
        return ranked

    monkeypatch.setattr(support_candidates, "_rank", locked_rank)
    started = perf_counter()
    try:
        with pytest.raises(RawRecallUnavailable):
            support_candidates.recover_support_candidates(runtime.store, query=QUERY, scope=owner(CANONICAL),
                context=CONTEXT, decision=gate.decision, excluded={exact_ref(first)}, maximum=1,
                deadline=started + .08)
        assert perf_counter() - started < .5, "exact hydration must not wait for the unrelated writer"
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)


def test_recovery_deadline_cannot_deliver_related_source_withdrawn_while_waiting(runtime, monkeypatch):
    first, second = setup(runtime)
    client = Client(no_support)
    configured(monkeypatch, client)
    original_rank = support_candidates._rank

    def withdraw_and_overrun(*args):
        first.status = "withdrawn"
        persist_mutation(runtime, first)
        sleep(.3)
        return original_rank(*args)

    monkeypatch.setattr(support_candidates, "_rank", withdraw_and_overrun)
    with verification_budget_scope(), caller_assistance.host_delivery_deadline(perf_counter() + .25):
        lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL), context=CONTEXT, limit=5)
    assert not client.calls
    assert lane.admission == {"status": "unavailable", "reason": "collection_deadline_exhausted"}
    assert not lane.evidence and not lane.support_evidence
    assert RELATED not in render_citation("", lane.to_dict(), max_chars=16000)


def test_recovery_known_snapshot_change_stops_before_model_request(runtime, monkeypatch):
    first, second = setup(runtime)
    client = Client(no_support)
    configured(monkeypatch, client)
    original_read = runtime.store.read_raw_citation_records
    reads = []

    def changed_snapshot(**kwargs):
        reads.append(kwargs)
        if len(reads) == 3:  # gate before + after, then recovery's own snapshot
            first.status = "withdrawn"
            persist_mutation(runtime, first)
        return original_read(**kwargs)

    monkeypatch.setattr(runtime.store, "read_raw_citation_records", changed_snapshot)
    with verification_budget_scope(), caller_assistance.host_delivery_deadline(perf_counter() + 3):
        lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL), context=CONTEXT, limit=5)
    assert len(reads) == 3
    assert not client.calls, "a known changed authority snapshot must not send old source text to a model"
    assert lane.admission == {"status": "unavailable", "reason": "support_recovery_source_changed"}
    assert not lane.evidence and not lane.support_evidence


def expiring_extra(runtime):
    first, second = setup(runtime)
    text = "Valid from: 2026-11-17T11:00:00Z\nValid until: 2026-11-17T12:01:00Z\n" + MISSING
    second.content["text"] = second.content["raw_text"] = second.summary = second.detail = text
    persist_mutation(runtime, second)
    return first, second


def expiry_response(packet, status):
    extra = [p["id"] for p in packet["passages"] if MISSING in p["text"]]
    assert extra, "the expiring extra must really participate in the model review"
    return {"request_digest": packet["request_digest"], "status": status,
            "reason": "direct_support" if status == "supported" else "missing_requested_fact",
            "selected": extra if status == "supported" else []}


@pytest.mark.parametrize("status", ["supported", "unsupported"])
def test_extra_expiring_during_model_invalidates_review_and_keeps_current_related(runtime, monkeypatch, wall_clock, status):
    first, second = expiring_extra(runtime)
    def response(packet):
        result = expiry_response(packet, status)
        wall_clock.current = EXPIRES + timedelta(seconds=1)
        return result
    client = Client(response)
    configured(monkeypatch, client)
    lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL), context=CONTEXT, limit=5)
    assert client.calls
    assert lane.answer_support["status"] == "unknown"
    assert lane.answer_support["reason"] == "support_applicability_changed"
    assert not lane.support_evidence
    assert [r["ref"][0] for r in lane.evidence] == [first.record_id]


@pytest.mark.parametrize("status", ["supported", "unsupported"])
def test_cached_extra_expiry_drops_review_but_keeps_unexpired_related_and_l1(runtime, monkeypatch, wall_clock, status):
    first, second = expiring_extra(runtime)
    configured(monkeypatch, Client(lambda packet: expiry_response(packet, status)))
    persona(runtime, CANONICAL, text="Prefer concise 上新后怎样告知同伴 summaries.")
    configure_proactive(runtime)
    payload = proactive(runtime, query=QUERY, base=CANONICAL)
    lane = payload["bundle"]["raw_citation"]
    assert lane["answer_support"]["status"] == status
    assert lane["answer_support"]["valid_until"] == EXPIRES.isoformat()
    assert [r["ref"][0] for r in lane["evidence"]] == [first.record_id]
    before = final_context(runtime, payload, query=QUERY, base=CANONICAL)
    assert "selected by a model" in before if status == "supported" else "reviewed candidate set does not support" in before
    wall_clock.current = EXPIRES + timedelta(seconds=1)
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    after = final_context(runtime, payload, query=QUERY, base=CANONICAL, expected_authority=expected)
    assert RELATED in after and "Prefer concise" in after
    assert "Answer support is unknown" in after
    assert "selected by a model" not in after and "reviewed candidate set does not support" not in after


def test_negative_review_expiry_is_bound_into_cached_sidecar(runtime, monkeypatch, wall_clock):
    expiring_extra(runtime)
    configured(monkeypatch, Client(lambda packet: expiry_response(packet, "unsupported")))
    persona(runtime, CANONICAL, text="Prefer concise 上新后怎样告知同伴 summaries.")
    configure_proactive(runtime)
    payload = proactive(runtime, query=QUERY, base=CANONICAL)
    assert payload["bundle"]["raw_citation"]["answer_support"]["valid_until"] == EXPIRES.isoformat()
    tampered = deepcopy(payload)
    tampered["bundle"]["raw_citation"]["answer_support"]["valid_until"] = "2099-01-01T00:00:00+00:00"
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    rendered = final_context(runtime, tampered, query=QUERY, base=CANONICAL, expected_authority=expected)
    assert RELATED not in rendered and "reviewed candidate set does not support" not in rendered
    assert "Prefer concise" in rendered


def test_historical_review_does_not_acquire_current_wall_clock_expiry(runtime, monkeypatch, wall_clock):
    expiring_extra(runtime)
    configured(monkeypatch, Client(lambda packet: expiry_response(packet, "unsupported")))
    query = "As of 2026-11-17T12:00:00Z, " + QUERY
    lane = collect_citation(runtime.memory, query=query, scope=owner(CANONICAL), context=CONTEXT, limit=5)
    assert lane.answer_support["status"] == "unsupported"
    assert not lane.answer_support.get("valid_until")
    wall_clock.current = EXPIRES + timedelta(days=1)
    rendered = render_citation("", lane.to_dict(), max_chars=16000)
    assert "reviewed candidate set does not support" in rendered


@pytest.mark.parametrize("overflow", ["parent", "total"])
def test_optional_extra_text_budget_cannot_invalidate_a_supported_original_packet(runtime, monkeypatch, overflow):
    answer = "上新后应在邮件列表告知同伴，并附本次发布说明链接。"
    if overflow == "total":
        answer += "p" * (12000 - len(answer))
    original = add_raw(runtime, answer, scope=owner(CANONICAL))
    extra_size = MAX_PARENT_CHARS + 1 if overflow == "parent" else 12000
    fillers = ("x",) if overflow == "parent" else ("x", "y")
    for char in fillers:
        add_raw(runtime, MISSING + char * (extra_size - len(MISSING)), scope=owner(CANONICAL))
    before = search_raw_chunks(runtime.store, query=QUERY, scope=owner(CANONICAL),
                               source_ids=["hermes"], task_context=CONTEXT, limit=5)
    assert [row["record"]["record_id"] for row in before] == [original.record_id]
    client = Client()
    configured(monkeypatch, client)
    lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL), context=CONTEXT, limit=5)
    assert len(client.calls) == 1
    packet = json.loads(client.calls[0]["user_prompt"])
    assert packet["passages"][0]["text"] == answer
    assert all(len(p["text"]) <= MAX_PARENT_CHARS for p in packet["passages"])
    assert sum(len(p["text"]) for p in packet["passages"]) <= MAX_TOTAL_CHARS
    assert len(packet["passages"]) == (1 if overflow == "parent" else 2)
    assert lane.answer_support["status"] == "supported"
    assert [row["ref"][0] for row in lane.evidence] == [original.record_id]
    assert [row["ref"][0] for row in lane.support_evidence] == [original.record_id]


@pytest.mark.parametrize("mutation", ["none", "withdrawal", "host_expired"])
def test_unknown_timed_out_review_requires_bounded_fresh_read_before_related_fallback(runtime, monkeypatch, mutation):
    text = "Sable storage manifests are maintained in the release repository."
    query = "Where are Sable storage manifests maintained?"
    record = add_raw(runtime, text, scope=owner(CANONICAL))
    events = []
    original_snapshot = raw_citation._same_request_snapshot
    started = perf_counter()
    collection_deadline = started + .2
    host_deadline = started + (.2 if mutation == "host_expired" else 2)

    class Timeout(Client):
        timeout_seconds = .25
        def complete(self, **kwargs):
            self.calls.append(kwargs)
            sleep(.26)
            if mutation == "withdrawal":
                record.status = "withdrawn"
                persist_mutation(runtime, record)
            raise TimeoutError("synthetic_transport_timeout")

    def snapshot(*args, **kwargs):
        events.append({"started": perf_counter(), "deadline": kwargs["deadline"]})
        return original_snapshot(*args, **kwargs)

    client = Timeout()
    configured(monkeypatch, client)
    monkeypatch.setattr(raw_citation, "_same_request_snapshot", snapshot)
    with verification_budget_scope(), caller_assistance.host_delivery_deadline(host_deadline):
        lane = collect_citation(runtime.memory, query=query, scope=owner(CANONICAL),
            context={**CONTEXT, "_recall_deadline_monotonic": collection_deadline}, limit=5)
    assert len(client.calls) == 1
    assert lane.answer_support["status"] == "unknown" and not lane.support_evidence
    if mutation == "host_expired":
        assert not events and not lane.evidence
    else:
        assert len(events) == 1
        assert collection_deadline < events[0]["started"] < events[0]["deadline"] <= host_deadline
        from eimemory.core.budgets import FINAL_AUTHORITY_SECONDS
        assert events[0]["deadline"] - events[0]["started"] <= FINAL_AUTHORITY_SECONDS
        if mutation == "withdrawal":
            assert not lane.evidence
            assert lane.admission["reason"] == "support_snapshot_changed"
        else:
            assert [row["ref"][0] for row in lane.evidence] == [record.record_id]
            rendered = render_citation("", lane.to_dict(), max_chars=16000)
            assert text in rendered and "Answer support is unknown" in rendered
