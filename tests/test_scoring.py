from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scoring import (
    ScoreContext,
    evaluate_memory_score,
    memory_score_to_legacy_quality,
    score_from_legacy_quality,
)
from eimemory.scoring.thresholds import tier_for_score


def test_memory_score_contract_uses_v1_formula_and_labels() -> None:
    score = evaluate_memory_score(
        text="User prefers concise spoken replies for project updates and durable agent behavior.",
        title="Reply style preference",
        memory_type="preference",
        source="runtime",
        context=ScoreContext(activity="runtime.ingest", source="runtime.ingest"),
    )

    payload = score.to_dict()

    assert payload["schema_version"] == "memory_score.v1"
    assert payload["tier"] in {"confirmed", "core"}
    assert payload["final_score"] >= 0.5
    assert payload["components"]["salience"]["value"] >= 0.6
    assert "lifecycle.confirmed" in payload["labels"] or "lifecycle.core" in payload["labels"]
    assert payload["provenance"]["activity"] == "memory.score"


def test_legacy_quality_maps_to_memory_score_and_back() -> None:
    record = RecordEnvelope.create(
        kind="memory",
        title="Migrated preference",
        summary="Keep replies concise and factual.",
        scope=ScopeRef(agent_id="main", workspace_id="repo-x"),
        meta={
            "quality": {
                "importance": 0.7,
                "confidence": 0.8,
                "freshness": 0.9,
                "reuse_potential": 0.6,
                "salience_score": 0.7,
                "quality_tier": "confirmed",
                "capture_decision": "accept",
            }
        },
    )

    score = score_from_legacy_quality(record=record, activity="quality.repair")
    round_tripped = memory_score_to_legacy_quality(score)

    assert score.components["confidence"].value == 0.8
    assert score.components["salience"].value == 0.7
    assert score.tier == "confirmed"
    assert round_tripped["quality_tier"] == "confirmed"
    assert round_tripped["capture_decision"] == "accept"
    assert round_tripped["salience_score"] == 0.7


def test_memory_score_tier_boundaries_follow_contract() -> None:
    assert tier_for_score(0.0) == "rejected"
    assert tier_for_score(0.24) == "rejected"
    assert tier_for_score(0.25) == "candidate"
    assert tier_for_score(0.49) == "candidate"
    assert tier_for_score(0.5) == "confirmed"
    assert tier_for_score(0.74) == "confirmed"
    assert tier_for_score(0.75) == "core"
    assert tier_for_score(1.0) == "core"
