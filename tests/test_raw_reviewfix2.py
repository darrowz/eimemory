"""Focused synthetic regressions for the final independent-review repair."""
from copy import deepcopy

import pytest

from eimemory.raw.retrieval import rerank_raw_results


def row(name, text, score, day=None, **extra):
    record = {"record_id": name, "kind": "raw_chunk", "text": text, **extra}
    if day is not None:
        record["occurred_at"] = day + "T00:00:00Z"
    return {"record": record, "base_score": score}


@pytest.mark.parametrize("scores,expected", [
    pytest.param((.5, .500001), ["second", "first"], id="compressed-near-tie"),
    pytest.param((.5, .5), ["first", "second"], id="true-tie"),
])
def test_equal_content_uses_exact_secondary_then_input_order(scores, expected):
    ranked = rerank_raw_results(query="unmatched", results=[
        row("first", "Plain evidence.", scores[0]),
        row("second", "Plain evidence.", scores[1]),
    ])
    assert [item["record"]["record_id"] for item in ranked] == expected
    assert ranked[0]["final_score"] == ranked[1]["final_score"] == 1.166667
    assert all(set(item) == {"record", "base_score", "final_score", "boosts"}
               for item in ranked)


@pytest.mark.parametrize("separator", [
    pytest.param(". ", id="english-sentence"),
    pytest.param(".", id="unspaced-sentence"),
    pytest.param("。", id="chinese-sentence"),
    pytest.param("; ", id="semicolon"),
    pytest.param("；", id="chinese-semicolon"),
    pytest.param(", ", id="comma-clause"),
    pytest.param("，", id="chinese-comma"),
    pytest.param("! ", id="exclamation"),
    pytest.param("？", id="chinese-question"),
    pytest.param(" and ", id="conjunction"),
    pytest.param(" but ", id="contrast-clause"),
    pytest.param(" while ", id="subordinate-clause"),
    pytest.param("但是", id="chinese-conjunction"),
    pytest.param(": ", id="colon"),
    pytest.param(" — ", id="dash"),
    pytest.param("\n", id="newline"),
])
@pytest.mark.parametrize("assertion", [
    pytest.param("Correction: Soren archive backend changed to MySQL.", id="correction"),
    pytest.param("Soren archive backend currently uses MySQL.", id="current"),
])
def test_marker_cannot_borrow_subject_from_another_clause(separator, assertion):
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend is PostgreSQL.", .9, "2026-01-01"),
        row("unrelated", "Rhea archive backend was SQLite" + separator + assertion,
            .1, "2026-02-01"),
    ])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not ranked[1]["boosts"].get("correction_marker")
    assert not ranked[1]["boosts"].get("current_fact")


@pytest.mark.parametrize("new_text", [
    'Rhea archive backend label is "correction".',
    'Rhea archive backend was SQLite. "Rhea archive backend currently uses MySQL."',
    '"Rhea archive backend was SQLite." Correction: Soren archive backend changed to MySQL.',
    'Rhea archive backend was SQLite.Correction: Soren archive backend changed to MySQL.',
    'Rhea archive backend was SQLite; Rhea queue backend changed to MySQL.',
    'Rhea archive backend was SQLite; Rhea queue backend currently uses MySQL.',
    'Rhea archive backend was SQLite (Correction: Soren archive backend changed to MySQL).',
    'Rhea archive backend was SQLite (Soren archive backend currently uses MySQL).',
], ids=["quoted-word", "quoted-current-prose", "quoted-old-prose", "dotted-marker-data",
        "other-attribute-change", "other-attribute-current", "parenthetic-change", "parenthetic-current"])
def test_quoted_markers_and_other_attributes_cannot_assert_a_correction(new_text):
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend is PostgreSQL.", .9, "2026-01-01"),
        row("unrelated", new_text, .1, "2026-02-01"),
    ])
    assert ranked[0]["record"]["record_id"] == "old"


@pytest.mark.parametrize("query,old,new", [
    pytest.param('"urn:svc.v2" backend', '"urn:svc.v2" backend uses SQLite.',
                 'Correction: "urn:svc.v2" backend changed to MySQL.', id="quoted-identifier"),
    pytest.param('"salt and pepper" backend', '"salt and pepper" backend uses SQLite.',
                 'Correction: "salt and pepper" backend changed to MySQL.', id="quoted-conjunction"),
    pytest.param('"red, blue" backend', '"red, blue" backend uses SQLite.',
                 'Correction: "red, blue" backend changed to MySQL.', id="quoted-comma"),
    pytest.param("engine 2.5 backend", "Engine 2.5 backend uses SQLite.",
                 "Correction: engine 2.5 backend changed to MySQL.", id="decimal-version"),
    pytest.param("service.v2 backend", "Service.v2 backend uses SQLite.",
                 "Correction: service.v2 backend changed to MySQL.", id="dotted-identifier"),
    pytest.param("青松项目后端", "青松项目后端使用 SQLite。",
                 "更正：青松项目后端改为 MySQL。", id="chinese-label"),
    pytest.param("Rhea archive backend", "Rhea archive backend uses SQLite.",
                 "Soren queue uses files but now Rhea archive backend uses MySQL.", id="but-now"),
])
def test_statement_binding_preserves_same_subject_literals(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .9, "2026-01-01"), row("new", new, .1, "2026-02-01"),
    ])
    assert ranked[0]["record"]["record_id"] == "new"
    assert ranked[0]["boosts"].get("correction_marker")


@pytest.mark.parametrize("text,metadata", [
    pytest.param("Rhea archive backend currently uses MySQL.", {}, id="text-current"),
    pytest.param("Rhea archive backend current=true uses MySQL.", {}, id="local-text-flag"),
    pytest.param("Rhea archive backend uses MySQL.", {"current": True}, id="metadata-bool"),
    pytest.param("Rhea archive backend uses MySQL.", {"current": "true"}, id="metadata-string"),
    pytest.param("青松 archive backend 当前使用 MySQL。", {}, id="chinese-text-current"),
])
def test_same_subject_current_assertions_preserve_inputs_and_payload(text, metadata):
    subject = "青松" if text.startswith("青松") else "Rhea"
    inputs = [
        row("old", subject + " archive backend uses SQLite.", .9, "2026-01-01"),
        row("new", text, .1, "2026-02-01", **metadata),
    ]
    before = deepcopy(inputs)
    ranked = rerank_raw_results(query=subject + " archive backend", results=inputs)
    assert ranked[0]["record"]["record_id"] == "new"
    assert ranked[0]["record"]["text"] == text
    assert ranked[0]["boosts"].get("current_fact")
    assert inputs == before


@pytest.mark.parametrize("metadata", [{}, {"current": True}], ids=["text", "metadata"])
def test_current_cannot_bypass_an_unbound_correction(metadata):
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend uses SQLite.", .9, "2026-01-01"),
        row("new", "Rhea archive backend currently uses MySQL. "
            "Correction: Soren archive backend changed to MySQL.", .1, "2026-02-01", **metadata),
    ])
    assert ranked[0]["record"]["record_id"] == "old"


def test_valid_from_is_a_fact_date_not_explicit_current_metadata():
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend uses SQLite.", .9, "2026-01-01"),
        row("new", "Rhea archive backend uses MySQL.", .1, valid_from="2026-02-01"),
    ])
    assert ranked[0]["record"]["record_id"] == "old"


@pytest.mark.parametrize("partition", ["source_id", "scope"], ids=["source", "scope"])
@pytest.mark.parametrize("current_metadata", [False, True], ids=["text-current", "metadata-current"])
def test_current_assertions_cannot_cross_exact_partitions(partition, current_metadata):
    left, right = ("left", "right") if partition == "source_id" else (
        {"user_id": "left"}, {"user_id": "right"})
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend uses SQLite.", .9, "2026-01-01", **{partition: left}),
        row("new", "Rhea archive backend currently uses MySQL.", .1, "2026-02-01",
            current=current_metadata, **{partition: right}),
    ])
    assert ranked[0]["record"]["record_id"] == "old"


@pytest.mark.parametrize("query", [
    "What is ticket 2025 status?", "What is parcel 2025 status?", "2025",
], ids=["ticket", "parcel", "numeric-only"])
def test_bare_calendar_shaped_numbers_remain_identifiers(query):
    ranked = rerank_raw_results(query=query, results=[
        row("old", "Ticket parcel 2025 status is open.", .9, "2026-01-01"),
        row("new", "Correction: ticket parcel 2025 status changed to resolved.", .1, "2026-02-01"),
    ])
    assert ranked[0]["record"]["record_id"] == "new"


@pytest.mark.parametrize("query", [
    "ticket status in 2025", "ticket status during 2025", "ticket status year 2025",
    "ticket status after 2025", "ticket status for 2025",
    "ticket status 2025年", "ticket status 2025-11", "historical ticket status",
    "ticket status before 2026-02-01", "ticket status as of 2026-01-15",
    "ticket status as of 2026-02-30",
], ids=["in-year", "during-year", "year-prefix", "after-year", "for-year", "chinese-unit",
        "year-month", "historical", "before", "as-of", "invalid-date"])
def test_calendar_context_and_unknown_cutoffs_remain_conservative(query):
    ranked = rerank_raw_results(query=query, results=[
        row("old", "Ticket status is open.", .9, "2026-01-01"),
        row("new", "Correction: ticket status changed to resolved.", .1, "2026-02-01", current=True),
    ])
    assert ranked[0]["record"]["record_id"] == "old"


@pytest.mark.parametrize("first_day,second_day", [(None, "2026-02-01"), ("2026-01-01", None)],
                         ids=["missing-old-day", "missing-new-day"])
def test_current_assertions_require_both_fact_dates(first_day, second_day):
    ranked = rerank_raw_results(query="Rhea archive backend", results=[
        row("old", "Rhea archive backend uses SQLite.", .9, first_day),
        row("new", "Rhea archive backend currently uses MySQL.", .1, second_day, current=True),
    ])
    assert ranked[0]["record"]["record_id"] == "old"
