from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eimemory.metadata import business_metadata, normalize_metadata
from eimemory.scoring.contract import MemoryScore, ScoreContext
from eimemory.scoring.evaluator import evaluate_memory_score
from eimemory.scoring.thresholds import capture_decision_for_tier

if TYPE_CHECKING:
    from eimemory.models.records import RecordEnvelope


SCORING_META_KEY = "scoring"
MEMORY_SCORE_META_KEY = "memory_score_v1"


def memory_score_to_legacy_quality(score: MemoryScore) -> dict[str, Any]:
    salience = score.components["salience"].value
    return {
        "importance": salience,
        "confidence": score.components["confidence"].value,
        "freshness": score.components["freshness"].value,
        "reuse_potential": score.components["reuse"].value,
        "salience_score": salience,
        "quality_tier": score.tier,
        "capture_decision": capture_decision_for_tier(score.tier),
    }


def _legacy_quality(meta: dict[str, Any] | None) -> dict[str, Any]:
    quality = business_metadata(meta).get("quality") or {}
    if not isinstance(quality, dict):
        return {}
    return dict(quality)


def score_from_legacy_quality(
    *,
    record: "RecordEnvelope",
    activity: str,
    source: str | None = None,
    profile: str = "balanced",
) -> MemoryScore:
    business_meta = business_metadata(record.meta)
    text = str(record.content.get("text") or record.summary or record.detail or record.title)
    memory_type = str(business_meta.get("memory_type") or record.content.get("memory_type") or "")
    return evaluate_memory_score(
        text=text,
        title=str(record.title or ""),
        memory_type=memory_type,
        source=str(record.source or source or ""),
        context=ScoreContext(
            activity=activity,
            profile=profile,
            source=str(source or activity),
            entity_id=str(record.record_id or ""),
        ),
        legacy_quality=_legacy_quality(record.meta),
    )


def score_payload(score: MemoryScore) -> dict[str, Any]:
    return score.to_dict()


def with_score_metadata(meta: dict[str, Any] | None, score: MemoryScore, *, preserve_quality: bool = False) -> dict[str, Any]:
    payload = normalize_metadata(meta or {})
    quality = _legacy_quality(payload)
    mapped_quality = memory_score_to_legacy_quality(score)
    if preserve_quality and quality:
        merged_quality = dict(mapped_quality)
        merged_quality.update(quality)
        payload["quality"] = merged_quality
    else:
        payload["quality"] = mapped_quality
    scoring_meta = dict(payload.get(SCORING_META_KEY) or {})
    scoring_meta[MEMORY_SCORE_META_KEY] = score_payload(score)
    payload[SCORING_META_KEY] = scoring_meta
    return normalize_metadata(payload)


def extract_memory_score(meta: dict[str, Any] | None) -> MemoryScore | None:
    scoring_meta = dict(business_metadata(meta).get(SCORING_META_KEY) or {})
    payload = scoring_meta.get(MEMORY_SCORE_META_KEY)
    if not isinstance(payload, dict):
        return None
    return MemoryScore.from_dict(payload)
