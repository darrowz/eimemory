"""Quoted prose cannot lend identity to an unrelated local assertion."""
from copy import deepcopy

import pytest

from eimemory.raw.retrieval import rerank_raw_results


def row(name, text, score, day, **extra):
    return {"record": {"record_id": name, "kind": "raw_chunk", "text": text,
                       "source_id": "synthetic", "scope": {"user_id": "synthetic"},
                       "occurred_at": day + "T00:00:00Z", **extra}, "base_score": score}


@pytest.mark.parametrize("query,old,new", [
    pytest.param("Rhea archive backend", "Rhea archive backend uses SQLite.",
        'The manual quotes "Rhea archive backend uses SQLite" alongside a correction to Soren queue.',
        id="parent-unpunctuated-prose"),
    pytest.param("Rhea archive backend", "Rhea archive backend uses SQLite.",
        'The manual quotes "Rhea archive backend" alongside a correction to Soren queue.',
        id="whole-subject-quotation"),
    pytest.param("Rhea archive backend", "Rhea archive backend uses SQLite.",
        'Correction: the manual quotes "Rhea archive backend uses SQLite" alongside Soren queue.',
        id="marker-before-quotation"),
    pytest.param('"Rhea" archive backend', 'The "Rhea" archive backend uses SQLite.',
        'The manual quotes "Rhea archive backend uses SQLite" alongside a correction to Soren queue.',
        id="queried-name-inside-prose"),
    pytest.param("云杉仓库开门时间", "云杉仓库开门时间是八点。",
        '手册引用「云杉仓库开门时间是八点」同时更正白桦仓库的联系人。',
        id="chinese-unpunctuated-prose"),
])
def test_quoted_prose_cannot_supply_correction_subject(query, old, new):
    inputs = [row("old", old, .9, "2026-01-01"), row("new", new, .1, "2026-02-01")]
    before = deepcopy(inputs)
    ranked = rerank_raw_results(query=query, results=inputs)
    assert [item["record"]["record_id"] for item in ranked] == ["old", "new"]
    assert not any(item["boosts"].get("correction_marker") for item in ranked)
    assert inputs == before
    assert ranked[1]["record"]["text"] == new


@pytest.mark.parametrize("text,metadata", [
    pytest.param('The manual quotes "Rhea archive backend uses SQLite" alongside Soren queue.',
        True, id="metadata-current"),
    pytest.param('The manual quotes "Rhea archive backend uses SQLite" alongside Soren queue currently using files.',
        False, id="text-current"),
    pytest.param('手册引用「云杉仓库开门时间是八点」同时描述白桦仓库。',
        True, id="chinese-metadata-current"),
])
def test_currentness_uses_the_same_own_assertion_boundary(text, metadata):
    chinese = text.startswith("手册")
    query = "云杉仓库开门时间" if chinese else "Rhea archive backend"
    old = "云杉仓库开门时间是八点。" if chinese else "Rhea archive backend uses SQLite."
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .9, "2026-01-01"),
        row("new", text, .1, "2026-02-01", current=metadata)])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("current_fact") for item in ranked)
    assert not any(item["boosts"].get("temporal_currentness") for item in ranked)


@pytest.mark.parametrize("query,old,new,metadata", [
    pytest.param('"Rhea" archive backend', 'The "Rhea" archive backend uses SQLite.',
        'Correction: the "Rhea" archive backend now uses PostgreSQL, not SQLite.',
        False, id="parent-quoted-name"),
    pytest.param("Rhea archive backend", 'The "Rhea" archive backend uses SQLite.',
        'Correction: the "Rhea" archive backend uses PostgreSQL.',
        False, id="name-quoted-only-in-evidence"),
    pytest.param('"云杉"仓库开门时间', '「云杉」仓库开门时间是八点。',
        '更正：「云杉」仓库开门时间改为九点。', False, id="chinese-quoted-name"),
    pytest.param("ticket 8 status", 'Ticket "8" status is open.',
        'Correction: ticket "8" status changed to resolved.', False, id="quoted-numeric-id"),
    pytest.param('"Rhea" archive backend', 'The "Rhea" archive backend uses SQLite.',
        'The "Rhea" archive backend uses PostgreSQL.', True, id="quoted-name-current-metadata"),
])
def test_quoted_names_retain_local_attribute_binding(query, old, new, metadata):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .9, "2026-01-01"),
        row("new", new, .1, "2026-02-01", current=metadata)])
    assert ranked[0]["record"]["record_id"] == "new"
    assert ranked[0]["record"]["text"] == new
    assert ranked[0]["boosts"]["current_fact" if metadata else "correction_marker"] == (.7 if metadata else .95)
