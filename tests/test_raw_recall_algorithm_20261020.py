"""Raw recall must ground, deduplicate and rank the complete candidate union."""
from contextlib import closing
from dataclasses import replace
import math

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw import retrieval
from eimemory.raw.store import RawEvidenceAPI
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(agent_id="honda", workspace_id="recall-audit")


@pytest.fixture
def raw_store(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        yield store


def ingest(store, text, event, *, occurred_at="2026-10-01T00:00:00Z"):
    return RawEvidenceAPI(store).ingest_text(
        text=text, scope=SCOPE, source_event_id=event, session_id=event,
        occurred_at=occurred_at,
    )[0]


def test_raw_search_uses_full_question_for_scoped_fts_rank(raw_store):
    query = "orchard kettle beetle chartreuse fern meadow planet apple bridge berry willow pear violet meadowlark cabin lake"
    # This word falls between sample positions in the default twelve-term arm.
    assert '"chartreuse"' not in raw_store.sqlite._fts_query(query)
    target = raw_store.append(RecordEnvelope.create(
        kind="raw_chunk", scope=SCOPE, source_id="handbook", title="Visit",
        content={"text": "chartreuse"},
    ))
    for index in range(24):
        raw_store.append(RecordEnvelope.create(
            kind="raw_chunk", scope=SCOPE, source_id="handbook", title="Visit",
            content={"text": f"orchard cabin {index}"},
        ))
    raw_store.append(RecordEnvelope.create(
        kind="raw_chunk", scope=SCOPE, source_id="unrelated", title=query,
        content={"text": query},
    ))

    hits = RawEvidenceAPI(raw_store).search_raw_chunks(
        query=query, scope=SCOPE, source_ids=["handbook"], limit=6,
    )
    assert [hit["record"].record_id for hit in hits if hit.get("fts_rank") == 1] == [target.record_id]
    assert all(hit["record"].source_id == "handbook" for hit in hits)


def test_raw_candidate_quota_retains_actual_fts_leaders(raw_store):
    rows = []
    for rank in range(6, 0, -1):
        record = RecordEnvelope.create(kind="raw_chunk", scope=SCOPE, title=str(rank))
        rows.append((rank, 0, record, {"fts_rank": rank, "candidate_sources": ["fts"]}))

    chosen = raw_store.sqlite._select_hybrid_with_fts_quota(rows, limit=4)
    assert [row[3]["fts_rank"] for row in chosen] == [6, 5, 2, 1]


def test_legacy_id_only_score_cannot_supply_raw_fts_rank(raw_store, monkeypatch):
    record = ingest(raw_store, "heron observatory", "observation")
    rating = {"record_id": record.record_id, "final_score": 0.75, "fts_rank": 1}
    monkeypatch.setattr(raw_store, "search_with_diagnostics", lambda **kw: (
        [record], {"scored_items": [rating]},
    ))

    hits = RawEvidenceAPI(raw_store).search_raw_chunks(query="heron", scope=SCOPE)
    assert hits == [{"record": record, "base_score": 0.75}]


def test_unknown_query_does_not_return_unrelated_raw_corrections(raw_store):
    ingest(raw_store, "更正：午餐改为面条，之前说错了。", "lunch")
    ingest(raw_store, "小马哥负责系统问题，鸿睿负责执行核查。", "team")

    assert retrieval.search_raw_chunks(
        raw_store, query="quuxnevermatchedword", scope=SCOPE, limit=3,
    ) == []


@pytest.mark.parametrize("restriction", ["source", "exact_scope"])
@pytest.mark.parametrize("query,answer,related", [
    ("amber depot custodian", "Amber depot keys are held by Lin.", "The depot meeting was postponed."),
    ("linden sensor interval", "The linden sensor samples every 45 seconds.", "The sensor arrived on Tuesday."),
    ("cobalt return address", "Ship cobalt returns to Dock 19.", "The return process was discussed."),
])
def test_boundary_precedes_raw_index_limit(raw_store, query, answer, related, restriction):
    owner = replace(SCOPE, user_id="operator")

    def add(kind, source, title, body, scope=owner):
        return raw_store.append(RecordEnvelope.create(
            kind=kind, scope=scope, source_id=source, title=title,
            content={"raw_text": body, "text": body},
        ))

    target = add("raw_chunk", "handbook", "Operations note", answer)
    add("memory", "handbook", "Meeting note", related)
    source, scope = ("archive", owner) if restriction == "source" else ("handbook", SCOPE)
    for i in range(8):
        add("raw_chunk", source, query, f"{query} (archive {i})", scope)

    hits = retrieval.search_raw_chunks(
        raw_store, query=query, scope=owner, source_ids=["handbook"], limit=1,
        task_context={"exact_scope_only": restriction == "exact_scope"},
    )
    assert [hit["record"]["record_id"] for hit in hits] == [target.record_id]


@pytest.mark.parametrize("query,text", [
    ("鸿睿", "鸿睿负责执行核查。"),
    ("记忆", "eimemory记忆召回由L1负责。"),
    ("L1", "eimemory记忆召回由L1负责。"),
])
def test_backstop_grounds_cjk_and_mixed_identifiers(raw_store, monkeypatch, query, text):
    target = ingest(raw_store, text, "target")
    monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(retrieval, "_store_raw_candidates", lambda *args, **kwargs: [])

    result = retrieval.search_raw_chunks(raw_store, query=query, scope=SCOPE, limit=2)

    assert [item["record"]["record_id"] for item in result] == [target.record_id]
    assert result[0]["base_score"] > 0
    assert result[0]["boosts"].get("keyword_overlap", 0) > 0


def test_backstop_correction_competes_with_indexed_stale_statement(raw_store, monkeypatch):
    old = ingest(raw_store, "鸿欣账号属于小马哥。", "old")
    correction = ingest(
        raw_store, "更正：鸿欣账号属于钊哥，不属于小马哥。", "correction",
        occurred_at="2026-10-02T00:00:00Z",
    )
    monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [
        {"record": old, "base_score": 1.0},
    ])

    results = retrieval.search_raw_chunks(
        raw_store, query="鸿欣账号归属", scope=SCOPE, limit=3,
    )

    assert [item["record"]["record_id"] for item in results] == [correction.record_id, old.record_id]
    assert results[0]["final_score"] >= results[1]["final_score"]
    assert results[0]["boosts"]["temporal_currentness"] > results[1]["boosts"]["temporal_currentness"]


def test_duplicate_index_hits_do_not_consume_delivery_slots(raw_store, monkeypatch):
    first = ingest(raw_store, "cedar primary evidence", "first")
    second = ingest(raw_store, "cedar secondary evidence", "second")
    monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [
        {"record": first, "base_score": 0.7},
        {"record": first, "base_score": 0.8},
        {"record": second, "base_score": 0.3},
    ])

    results = retrieval.search_raw_chunks(raw_store, query="cedar", scope=SCOPE, limit=2)

    assert len(results) == 2
    assert {item["record"]["record_id"] for item in results} == {first.record_id, second.record_id}
    assert next(item for item in results if item["record"]["record_id"] == first.record_id)["base_score"] == 0.8


def test_complete_turn_window_does_not_require_an_unavailable_backstop(raw_store, monkeypatch):
    raw = RawEvidenceAPI(raw_store)
    anchor = raw.ingest_text(text="cedar mission briefing", scope=SCOPE,
        source_event_id="anchor", session_id="mission", turn_id="turn:1")[0]
    neighbor = raw.ingest_text(text="The next step needs the same context.", scope=SCOPE,
        source_event_id="neighbor", session_id="mission", turn_id="turn:2")[0]
    monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [
        {"record": anchor, "base_score": 1.0},
    ])

    def unavailable_backstop(*args, **kwargs):
        from eimemory.raw.boundary import RawRecallUnavailable
        raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)

    monkeypatch.setattr(retrieval, "_direct_raw_scan_candidates", unavailable_backstop)
    results = retrieval.search_raw_chunks(raw_store, query="cedar mission", scope=SCOPE,
        limit=2, task_context={"task_type": "locomo", "granularity": "turn"})

    assert [item["record"]["record_id"] for item in results] == [anchor.record_id, neighbor.record_id]


@pytest.mark.parametrize("session,turn,rank,query,context,should_promote", [
    ("workshop", 0, 5, "orchard fault", {}, True),
    ("workshop", "2", 5, "orchard fault", {}, True),
    ("other", "2", 5, "orchard fault", {}, False),
    ("workshop", "turn-hash:2", 5, "orchard fault", {}, False),
    ("workshop", "2", 18, "orchard fault", {}, False),
    ("workshop", "2", None, "orchard fault", {}, False),
    ("workshop", "2", 5, "orchard fault on 2025-05-01", {}, False),
    ("workshop", "2", 5, "orchard fault", {"as_of": "2025-05-01"}, False),
])
def test_dialogue_window_delivers_only_scoped_grounded_adjacent_turn(
        raw_store, monkeypatch, session, turn, rank, query, context, should_promote):
    raw = RawEvidenceAPI(raw_store)
    hits = []
    for i, (conversation, ordinal, fts_rank) in enumerate([
        ("workshop", "1", 1), ("trip", "1", 2), ("inventory", "9", 3), (session, turn, rank),
    ]):
        record = raw.ingest_chunk({"session_id": conversation, "turn_id": ordinal, "role": "user",
            "text": f"Orchard fault report {i}."}, scope=SCOPE)
        hits.append({"record": record, "base_score": .8, "fts_rank": fts_rank})
    monkeypatch.setattr(retrieval, "_raw_api_search", lambda *a, **kw: hits)
    got = retrieval.search_raw_chunks(raw_store, query=query, scope=SCOPE, limit=3, task_context=context)
    assert [h["record"]["record_id"] for h in got] == [
        hits[i]["record"].record_id for i in (0, 1, 3 if should_promote else 2)]
    assert bool(got[-1]["boosts"].get("turn_context_neighbor")) == should_promote


@pytest.mark.parametrize("score", [float("inf"), "1e10000", 10 ** 400])
def test_nonfinite_provider_scores_cannot_dominate_or_break_ranking(raw_store, score):
    poisoned = ingest(raw_store, "cedar weak evidence", "poisoned")
    good = ingest(raw_store, "cedar strong evidence", "good")

    results = retrieval.rerank_raw_results(query="cedar", results=[
        {"record": poisoned, "base_score": score},
        {"record": good, "base_score": 1.0},
    ])

    assert results[0]["record"]["record_id"] == good.record_id
    assert all(math.isfinite(item["final_score"]) and math.isfinite(item["base_score"]) for item in results)
