"""Correction discourse may introduce one assertion, never another subject."""
import pytest

from eimemory.raw.retrieval import rerank_raw_results


def row(name, text, score, day):
    return {"record": {"record_id": name, "kind": "raw_chunk", "text": text,
                       "source_id": "synthetic", "scope": {"user_id": "synthetic"},
                       "occurred_at": day + "T00:00:00Z"}, "base_score": score}


@pytest.mark.parametrize("query,old,new", [
    pytest.param("云杉账号归属", "云杉账号属于甲组。",
                 "更正：刚才串档了，云杉账号属于乙组，不属于甲组。", id="chinese-ownership"),
    pytest.param('"Orchid 7" archive backend', '"Orchid 7" archive backend uses SQLite.',
                 'Correction: I was mistaken, "Orchid 7" archive backend uses MySQL.',
                 id="english-quoted-identity"),
    pytest.param("ticket 2084 status", "Ticket 2084 status is open.",
                 "Correction, ticket 2084 status is resolved.", id="numeric-identity"),
    pytest.param("云杉 archive backend", "云杉 archive backend uses SQLite.",
                 "纠正：弄错了，云杉 archive backend uses MySQL.", id="mixed-language"),
])
def test_correction_preamble_binds_immediate_complete_identity(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, 1.0, "2026-09-01"), row("new", new, .1, "2026-09-02")])
    assert [item["record"]["record_id"] for item in ranked] == ["new", "old"]
    assert ranked[0]["boosts"]["correction_marker"] == .95
    assert ranked[0]["final_score"] > ranked[1]["final_score"]
    assert ranked[0]["record"]["text"] == new


@pytest.mark.parametrize("new", [
    'Correction: I was mistaken, Birch archive backend uses MySQL. Orchid archive backend uses files.',
    'Correction: Birch archive backend uses MySQL, Orchid archive backend uses files.',
    'Correction: I was mistaken. Orchid archive backend uses MySQL.',
    'Correction: I was mistaken; Orchid archive backend uses MySQL.',
    'Correction: I was mistaken\nOrchid archive backend uses MySQL.',
    'Correction: I was mistaken, Orchid queue backend uses MySQL. Orchid archive backend uses files.',
    'Correction: I was mistaken, "Orchid archive backend uses MySQL."',
], ids=["other-subject", "factual-preamble", "sentence", "semicolon", "newline",
        "other-attribute", "quoted-prose"])
def test_correction_preamble_cannot_borrow_identity_or_cross_boundaries(new):
    ranked = rerank_raw_results(query="Orchid archive backend", results=[
        row("old", "Orchid archive backend uses SQLite.", 1.0, "2026-09-01"),
        row("new", new, .1, "2026-09-02")])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("correction_marker") for item in ranked)


def test_chinese_preamble_does_not_bridge_other_owner_or_sentence():
    ranked = rerank_raw_results(query="云杉账号归属", results=[
        row("old", "云杉账号属于甲组。", 1.0, "2026-09-01"),
        row("new", "更正：刚才串档了，白桦账号属于乙组，云杉账号属于甲组。",
            .1, "2026-09-02")])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("correction_marker") for item in ranked)
