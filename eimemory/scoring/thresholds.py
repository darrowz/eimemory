from __future__ import annotations


DEFAULT_PROFILE = "balanced"

DEFAULT_WEIGHTS: dict[str, float] = {
    "relevance": 0.22,
    "confidence": 0.16,
    "salience": 0.22,
    "freshness": 0.08,
    "provenance": 0.14,
    "reuse": 0.18,
    "risk_penalty": 0.35,
}

SCORING_PROFILES: dict[str, dict[str, float]] = {
    "balanced": dict(DEFAULT_WEIGHTS),
    "precision": {
        "relevance": 0.18,
        "confidence": 0.2,
        "salience": 0.18,
        "freshness": 0.06,
        "provenance": 0.2,
        "reuse": 0.18,
        "risk_penalty": 0.45,
    },
    "exploration": {
        "relevance": 0.28,
        "confidence": 0.12,
        "salience": 0.2,
        "freshness": 0.1,
        "provenance": 0.1,
        "reuse": 0.2,
        "risk_penalty": 0.2,
    },
}

TIER_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.75, "core"),
    (0.5, "confirmed"),
    (0.25, "candidate"),
    (0.0, "rejected"),
)


def clamp_score(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return round(max(0.0, min(1.0, numeric)), 4)


def weights_for_profile(profile: str | None) -> dict[str, float]:
    name = str(profile or DEFAULT_PROFILE).strip().lower()
    return dict(SCORING_PROFILES.get(name) or DEFAULT_WEIGHTS)


def tier_for_score(score: float) -> str:
    bounded = clamp_score(score)
    for minimum, tier in TIER_THRESHOLDS:
        if bounded >= minimum:
            return tier
    return "rejected"


def capture_decision_for_tier(tier: str) -> str:
    return "reject" if str(tier or "").strip().lower() == "rejected" else "accept"
