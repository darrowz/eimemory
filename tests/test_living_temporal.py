import pytest

from eimemory.living.temporal import (
    ANTICIPATES,
    BELONGS_TO_PHASE,
    REINFORCES,
    REPAIRS,
    REPEATS,
    SUPERSEDES,
    TEMPORAL_RELATIONS,
    build_temporal_link,
    build_temporal_relation_payload,
    summarize_timeline,
)
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


def _record(
    record_id: str,
    *,
    title: str | None = None,
    links: list[LinkRef] | None = None,
    meta: dict | None = None,
) -> RecordEnvelope:
    return RecordEnvelope(
        record_id=record_id,
        kind="memory",
        status="active",
        title=title or record_id,
        summary="",
        detail="",
        content={},
        tags=[],
        links=list(links or []),
        evidence=[],
        source="test",
        scope=ScopeRef(),
        time=TimeRef(created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z", occurred_at="2026-01-01T00:00:00Z"),
        provenance={},
        meta=dict(meta or {}),
    )


def _living_temporal(**temporal: object) -> dict:
    return {"living_memory_v1": {"temporal": temporal}}


def test_canonical_temporal_relation_names_are_stable() -> None:
    assert TEMPORAL_RELATIONS == (
        "supersedes",
        "repeats",
        "belongs_to_phase",
        "anticipates",
        "repairs",
        "reinforces",
    )
    assert {
        SUPERSEDES,
        REPEATS,
        BELONGS_TO_PHASE,
        ANTICIPATES,
        REPAIRS,
        REINFORCES,
    } == set(TEMPORAL_RELATIONS)


def test_build_temporal_link_validates_canonical_relations() -> None:
    link = build_temporal_link(SUPERSEDES, target_id="memory-old")

    assert link == LinkRef(relation="supersedes", target_kind="record", target_id="memory-old")

    with pytest.raises(ValueError, match="unknown temporal relation"):
        build_temporal_link("replaces", target_id="memory-old")


def test_build_temporal_relation_payload_uses_relation_record_shape() -> None:
    payload = build_temporal_relation_payload(
        SUPERSEDES,
        subject_id="memory-new",
        object_id="memory-old",
        evidence_text="new preference corrects the earlier one",
        confidence=1.4,
        metadata={"source": "reflection"},
    )

    assert payload["relation_record_id"] == "temporal_relation:memory-new:supersedes:memory-old"
    assert payload["paper_source_id"] == "living_memory_v1"
    assert payload["subject_id"] == "memory-new"
    assert payload["object_id"] == "memory-old"
    assert payload["relation_type"] == "supersedes"
    assert payload["evidence_text"] == "new preference corrects the earlier one"
    assert payload["confidence"] == 1.0
    assert payload["metadata"] == {"source": "reflection"}
    assert payload["provenance"]["paper_source_id"] == "living_memory_v1"


def test_summarize_timeline_groups_phase_and_recurrence_without_living_meta() -> None:
    records = [
        _record("m1", meta=_living_temporal(life_phase="arrival", recurrence="daily")),
        _record("m2", meta=_living_temporal(life_phase="arrival", recurrence="weekly")),
        _record("m3", meta=_living_temporal(life_phase="settled", recurrence="daily")),
        _record("m4"),
        _record("m5", meta={"living_memory_v1": "malformed"}),
    ]

    summary = summarize_timeline(records)

    assert summary["phase_counts"] == {"arrival": 2, "settled": 1}
    assert summary["recurrence_counts"] == {"daily": 2, "weekly": 1}


def test_summarize_timeline_extracts_open_future_intents() -> None:
    records = [
        _record(
            "future-open",
            title="Try SQLite compaction",
            meta=_living_temporal(
                life_phase="next",
                recurrence="once",
                future_intent={"status": "open", "intent": "try SQLite compaction"},
            ),
        ),
        _record(
            "future-closed",
            title="Archive old note",
            meta=_living_temporal(future_intent={"status": "closed", "intent": "archive old note"}),
        ),
        _record("plain"),
    ]

    summary = summarize_timeline(records)

    assert summary["open_future_intents"] == [
        {
            "record_id": "future-open",
            "title": "Try SQLite compaction",
            "life_phase": "next",
            "recurrence": "once",
            "intent": "try SQLite compaction",
        }
    ]


def test_summarize_timeline_counts_unresolved_repairs_and_supersession_map() -> None:
    records = [
        _record("old"),
        _record("new", links=[build_temporal_link(SUPERSEDES, "old")]),
        _record("repair-open", links=[build_temporal_link(REPAIRS, "old")], meta=_living_temporal(repair={"status": "unresolved"})),
        _record("repair-done", links=[build_temporal_link(REPAIRS, "new")], meta=_living_temporal(repair={"status": "resolved"})),
    ]

    summary = summarize_timeline(records)

    assert summary["supersession_map"] == {"old": "new"}
    assert summary["unresolved_repair_count"] == 1
