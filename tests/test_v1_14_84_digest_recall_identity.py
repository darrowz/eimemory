"""1.14.84: daily research digests must be distinguishable and not re-dated copies."""
from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.knowledge.synthesis import build_research_digest, digest_to_record
from eimemory.models.records import RecordEnvelope, ScopeRef

SCOPE = {"agent_id": "main", "workspace_id": "papers"}


def _claim(rid, text, confidence, published):
    return RecordEnvelope.from_dict({
        "record_id": rid, "kind": "claim_card", "status": "active", "title": text[:20], "summary": text,
        "source": "eimemory.knowledge.compiler", "scope": SCOPE,
        "content": {"claim_text": text, "published_at": published, "paper_source_id": "psrc_" + rid},
        "meta": {"confidence": confidence, "source_kind": "arxiv"},
        "provenance": {"source": "arxiv", "published_at": published},
    })


def _digest(claims, date):
    return build_research_digest(paper_sources=[], claim_cards=claims, knowledge_pages=[], digest_date=date)


def test_notable_claim_tracks_recent_claims_not_all_time_top_confidence():
    old = _claim("old", "Old high confidence claim", 0.99, "2026-10-01")
    new = _claim("new", "Fresh claim from today", 0.7, "2026-10-14")
    digest = _digest([old, new], "2026-10-14")
    assert digest["notable_claims"][0]["text"] == "Fresh claim from today"
    assert "Fresh claim from today" in digest["summary"]


def test_summary_never_renders_an_empty_paper_list():
    digest = _digest([_claim("c", "Some claim", 0.8, "2026-10-14")], "2026-10-14")
    assert "Recent papers: ." not in digest["summary"]
    assert "Recent papers: none passed the evidence gate." in digest["summary"]


def test_unchanged_digest_is_not_persisted_again(tmp_path):
    runtime = Runtime.create(root=tmp_path / "rt")
    try:
        runtime.store.append(_claim("c1", "Stable claim", 0.8, "2026-10-10"))
        first = runtime.build_research_digest(scope=SCOPE, persist=True, digest_date="2026-10-13")
        assert first["persisted"] is True
        second = runtime.build_research_digest(scope=SCOPE, persist=True, digest_date="2026-10-14")
        assert second["persisted"] is False
        assert second["persist_skipped_reason"] == "unchanged_since_previous_digest"
        assert second["previous_digest_id"] == first["persisted_page_id"]
        runtime.store.append(_claim("c2", "New claim", 0.8, "2026-10-14"))
        third = runtime.build_research_digest(scope=SCOPE, persist=True, digest_date="2026-10-14")
        assert third["persisted"] is True
    finally:
        runtime.close()


def _page(date, created, claims):
    record = digest_to_record(_digest(claims, date), scope=SCOPE)
    payload = record.to_dict()
    payload["time"] = {**payload.get("time", {}), "created_at": created, "updated_at": created, "occurred_at": created}
    return RecordEnvelope.from_dict(payload)


def test_identical_body_digest_copies_collapse_to_newest():
    claims = [_claim("c", "Same claim every day", 0.8, "2026-10-01")]
    older = _page("2026-10-11", "2026-10-11T19:32:57Z", claims)
    newer = _page("2026-10-14", "2026-10-14T19:33:10Z", claims)
    assert older.record_id != newer.record_id
    kept = MemoryAPI._dedupe_records([older, newer])
    assert [item.record_id for item in kept] == [newer.record_id]
    kept = MemoryAPI._dedupe_records([newer, older])
    assert [item.record_id for item in kept] == [newer.record_id]


def test_distinct_digests_are_not_collapsed():
    a = _page("2026-10-13", "2026-10-13T19:00:00Z", [_claim("a", "Claim A", 0.8, "2026-10-13")])
    b = _page("2026-10-14", "2026-10-14T19:00:00Z", [_claim("b", "Claim B", 0.8, "2026-10-14")])
    assert len(MemoryAPI._dedupe_records([a, b])) == 2


def test_confidence_still_ranks_claims_inside_the_digest_window():
    a = _claim("a", "Lower confidence recent", 0.6, "2026-10-14")
    b = _claim("b", "Higher confidence this week", 0.9, "2026-10-10")
    assert _digest([a, b], "2026-10-14")["notable_claims"][0]["text"] == "Higher confidence this week"


def test_quiet_window_falls_back_to_all_time_claims():
    old = _claim("old", "Only claim", 0.8, "2026-09-01")
    assert _digest([old], "2026-10-14")["notable_claims"][0]["text"] == "Only claim"
