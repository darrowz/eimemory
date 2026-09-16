from __future__ import annotations

from collections import Counter
from statistics import mean

from eimemory.scoring.contract import MemoryScore


def summarize_scores(scores: list[MemoryScore]) -> dict:
    if not scores:
        return {
            "count": 0,
            "tiers": {},
            "average_final_score": 0.0,
            "average_components": {},
            "label_counts": {},
        }
    tiers = Counter(score.tier for score in scores)
    labels = Counter(label for score in scores for label in score.labels)
    # RSC-20: union keys across scores; missing components contribute 0.0.
    component_names = tuple(sorted({name for score in scores for name in score.components}))
    averages = {
        name: round(
            mean(
                (score.components[name].value if name in score.components else 0.0)
                for score in scores
            ),
            4,
        )
        for name in component_names
    }
    return {
        "count": len(scores),
        "tiers": dict(tiers),
        "average_final_score": round(mean(score.final_score for score in scores), 4),
        "average_components": averages,
        "label_counts": dict(labels),
    }
