"""Pure metadata transformations and copied-value consistency."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from eimemory.living.schema import enrich_living_memory, get_living_memory_meta


@pytest.mark.parametrize("field", ["text", "body", "raw_text"])
def test_mapping_content_matches_equivalent_record_object(field):
    content = {field: "Tomorrow I will write a concise summary."}
    mapping = {"title": "Note", "content": content}
    record = SimpleNamespace(title="Note", summary="", detail="", content=content, meta={})
    assert enrich_living_memory(mapping) == enrich_living_memory(record)


def test_occurrence_and_validity_are_preserved_separately():
    result = enrich_living_memory("A meeting note", meta={
        "occurred_at": "2026-09-01", "valid_from": "2026-10-01", "valid_until": "2026-10-31"})
    assert result["temporal"]["occurred_at"] == "2026-09-01"
    assert result["temporal"]["valid_from"] == "2026-10-01"
    assert result["temporal"]["valid_until"] == "2026-10-31"


def test_record_time_validity_is_preserved():
    result = enrich_living_memory({"text": "A note", "time": {
        "occurred_at": "2026-09-01", "valid_from": "2026-10-01", "valid_until": "2026-10-31"}})
    assert result["temporal"]["valid_from"] == "2026-10-01"
    assert result["temporal"]["valid_until"] == "2026-10-31"


def test_returned_living_metadata_does_not_alias_nested_source_values():
    meta = {"living_memory_v1": {"temporal": {"supersedes": ["old"],
        "future_intent": {"status": "open"}}, "motive": {"boundary": ["no_fluff"]}}}
    before = deepcopy(meta)
    result = get_living_memory_meta(meta)
    result["temporal"]["supersedes"].append("another")
    result["temporal"]["future_intent"]["status"] = "closed"
    result["motive"]["boundary"].clear()
    assert meta == before


def test_stale_future_statement_is_not_an_open_future_intent():
    result = enrich_living_memory("I no longer plan to send it tomorrow")
    assert result["temporal"]["temporal_distance"] == "stale"
    assert result["temporal"]["future_intent"]["status"] == "closed"


def test_validity_only_metadata_does_not_invent_occurrence():
    result = enrich_living_memory("A note", meta={"valid_from": "2026-10-01"})
    assert result["temporal"]["occurred_at"] == ""
    assert result["temporal"]["valid_from"] == "2026-10-01"


@pytest.mark.parametrize("section", ["temporal", "motive", "affective", "perspective", "action_posture", "quality_snapshot"])
@pytest.mark.parametrize("malformed", [None, "unexpected", 7, []])
def test_malformed_known_sections_keep_mapping_defaults(section, malformed):
    result = get_living_memory_meta({"living_memory_v1": {section: malformed}})
    assert isinstance(result[section], dict)
    assert result[section]


@pytest.mark.parametrize("text", ["The snow is deep", "I enjoy snowboarding", "We voted against it"])
def test_short_temporal_markers_do_not_match_inside_other_words(text):
    result = enrich_living_memory(text)
    assert result["temporal"]["temporal_distance"] == "unspecified"
    assert result["temporal"]["recurrence"] == "none"
    assert result["affective"]["pressure"] == "normal"
    assert result["affective"]["frustration_repeat"] is False


def test_standalone_short_temporal_markers_still_match():
    result = enrich_living_memory("Do it now, and again tomorrow")
    assert result["temporal"]["recurrence"] == "recurring"
    assert result["affective"]["pressure"] == "elevated"
