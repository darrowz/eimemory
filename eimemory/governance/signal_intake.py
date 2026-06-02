from __future__ import annotations

from typing import Any


def rank_learning_signals(
    signals: list[dict[str, Any]],
    self_model: dict[str, Any] | None = None,
    user_goals: list[dict[str, Any]] | None = None,
    *,
    max_items: int = 20,
) -> list[dict[str, Any]]:
    weakness_terms = _weakness_terms(self_model or {})
    goal_terms = _goal_terms(user_goals or [])
    ranked: list[dict[str, Any]] = []
    for signal in signals:
        payload = dict(signal or {})
        text = " ".join(str(payload.get(key) or "") for key in ("title", "summary", "signal_type", "source_kind")).lower()
        repeated = int(payload.get("repeat_count") or payload.get("count") or 1)
        evidence_tier = str(payload.get("evidence_tier") or _default_tier(payload)).upper()
        relevance = _bounded(0.35 + (0.25 if any(term in text for term in weakness_terms) else 0.0) + (0.15 if any(term in text for term in goal_terms) else 0.0))
        impact = _bounded(float(payload.get("impact") or 0.45) + min(0.25, repeated * 0.05))
        urgency = _bounded(float(payload.get("urgency") or 0.35) + (0.2 if _is_failure(payload) else 0.0))
        learnability = _bounded(float(payload.get("learnability") or 0.6) - (0.2 if _is_l3(payload) else 0.0))
        risk = _bounded(float(payload.get("risk") or (0.75 if _is_l3(payload) else 0.25)))
        confidence = _bounded(float(payload.get("confidence") or 0.55) + _tier_bonus(evidence_tier))
        score = _bounded(
            relevance * 0.25
            + impact * 0.24
            + urgency * 0.16
            + learnability * 0.16
            + confidence * 0.14
            - risk * 0.12
            + min(0.08, repeated * 0.02)
        )
        ranked.append(
            {
                **payload,
                "score": round(score, 3),
                "relevance": round(relevance, 3),
                "impact": round(impact, 3),
                "urgency": round(urgency, 3),
                "learnability": round(learnability, 3),
                "risk": round(risk, 3),
                "confidence": round(confidence, 3),
                "evidence_tier": evidence_tier,
            }
        )
    return sorted(ranked, key=lambda item: (-float(item.get("score") or 0.0), str(item.get("title") or "")))[: max(0, int(max_items))]


def _weakness_terms(self_model: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for weakness in self_model.get("weaknesses") or []:
        for key in ("kind", "capability", "lesson"):
            terms.update(_tokens(weakness.get(key)))
    return terms


def _goal_terms(goals: list[dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    for goal in goals:
        for key in ("title", "question", "target_capability"):
            terms.update(_tokens(goal.get(key)))
    return terms


def _tokens(value: Any) -> set[str]:
    return {part for part in str(value or "").lower().replace(".", " ").replace("_", " ").split() if len(part) >= 4}


def _default_tier(signal: dict[str, Any]) -> str:
    source_kind = str(signal.get("source_kind") or signal.get("source") or "").lower()
    if source_kind.startswith(("local_outcome", "outcome")):
        return "T0"
    if source_kind.startswith(("local_eval", "eval")):
        return "T1"
    if source_kind.startswith(("local", "repo", "tool")):
        return "T2"
    if source_kind.startswith(("github", "docs")):
        return "T3"
    if source_kind.startswith(("research", "paper")):
        return "T4"
    return "T5"


def _tier_bonus(tier: str) -> float:
    return {"T0": 0.25, "T1": 0.2, "T2": 0.12, "T3": 0.1, "T4": 0.06, "T5": 0.0, "T6": -0.2}.get(tier.upper(), 0.0)


def _is_failure(signal: dict[str, Any]) -> bool:
    text = " ".join(str(signal.get(key) or "") for key in ("title", "summary", "signal_type")).lower()
    return any(term in text for term in ("bad", "fail", "failure", "miss", "error", "regression", "失败", "错误"))


def _is_l3(signal: dict[str, Any]) -> bool:
    tier = str(signal.get("authority_tier") or "").upper()
    return tier == "L3"


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
