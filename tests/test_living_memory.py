from __future__ import annotations

from eimemory.living import (
    ACTION_POSTURES,
    LIVING_MEMORY_META_KEY,
    enrich_living_memory,
    get_living_memory_meta,
    with_living_memory_meta,
)
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def _memory(text: str, *, meta: dict | None = None) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title="Living memory fixture",
        summary=text,
        content={"text": text},
        scope=ScopeRef(agent_id="tester"),
        meta=meta or {"force_capture": True},
    )


def test_concise_no_fluff_text_detects_efficiency_and_no_fluff() -> None:
    living = enrich_living_memory("Prefer concise answers. No fluff, get straight to the point.")

    assert living["motive"]["motive"] == "efficiency"
    assert "no_fluff" in living["motive"]["boundary"]
    assert living["action_posture"]["naturalness"] == "concise"


def test_trust_failure_text_sets_negative_trust_delta_and_repair_needed() -> None:
    living = enrich_living_memory("You broke my trust by ignoring the boundary again; repair this before proceeding.")

    assert living["motive"]["trust_delta"] < 0
    assert living["affective"]["repair_needed"] is True
    assert living["action_posture"]["trust_risk"] == "high"
    assert living["action_posture"]["recommended"] == "act"


def test_positive_reinforcement_text_increases_trust_building() -> None:
    living = enrich_living_memory("Good, that helped. Keep doing that because it builds trust.")

    assert living["motive"]["trust_delta"] > 0
    assert living["affective"]["trust_building"] is True
    assert living["affective"]["valence"] == "positive"
    assert living["action_posture"]["recommended"] == "nudge"


def test_perspective_views_do_not_copy_source_text() -> None:
    text = "I feel frustrated when the answer repeats filler instead of acting."
    living = enrich_living_memory(text)

    assert living["perspective"]["self_view"] != text
    assert living["perspective"]["observer_view"] != text
    assert living["perspective"]["compassion_reframe"] != text
    assert len(
        {
            living["perspective"]["self_view"],
            living["perspective"]["observer_view"],
            living["perspective"]["compassion_reframe"],
        }
    ) == 3


def test_legacy_no_meta_returns_safe_defaults() -> None:
    record = RecordEnvelope.from_dict(
        {
            "record_id": "mem_legacy",
            "kind": "memory",
            "title": "Legacy",
            "summary": "old record",
            "scope": {},
            "meta": {},
        }
    )

    living = get_living_memory_meta(record)

    assert living["schema_version"] == "living_memory.v1"
    assert living["motive"]["trust_delta"] == 0
    assert living["affective"]["repair_needed"] is False
    assert LIVING_MEMORY_META_KEY not in record.meta


def test_with_living_memory_meta_preserves_existing_legacy_meta() -> None:
    record = _memory("No fluff and concise please.", meta={"legacy_key": "keep", "force_capture": True})

    enriched = with_living_memory_meta(record)

    assert record.meta["legacy_key"] == "keep"
    assert enriched["legacy_key"] == "keep"
    assert enriched[LIVING_MEMORY_META_KEY]["motive"]["motive"] == "efficiency"


def test_action_posture_recommended_uses_canonical_posture_values() -> None:
    for text in (
        "Prefer concise answers. No fluff.",
        "Good, keep doing that because it builds trust.",
        "You broke my trust; repair this before proceeding.",
        "Let go of that old preference; it is no longer relevant.",
        "Sparse neutral note.",
    ):
        living = enrich_living_memory(text)

        assert living["action_posture"]["recommended"] in ACTION_POSTURES


def test_let_go_language_maps_to_let_go_action_posture() -> None:
    living = enrich_living_memory("Let go of that old preference; it is no longer relevant.")

    assert living["action_posture"]["recommended"] == "let_go"
    assert living["temporal"]["temporal_distance"] == "stale"


def test_runtime_ingest_and_observe_auto_attach_living_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "living", "user_id": "darrow"}

    memory = runtime.memory.ingest(
        text="Prefer concise answers. No fluff.",
        memory_type="preference",
        title="Concise style",
        scope=scope,
        force_capture=True,
    )
    incident = runtime.evolution.observe(
        signal_type="incident",
        payload={"title": "Trust repair", "summary": "You broke my trust; repair this before proceeding."},
        scope=scope,
    )

    assert memory.meta[LIVING_MEMORY_META_KEY]["motive"]["motive"] == "efficiency"
    assert incident.meta[LIVING_MEMORY_META_KEY]["affective"]["repair_needed"] is True
