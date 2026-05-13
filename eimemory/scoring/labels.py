from __future__ import annotations

from eimemory.scoring.contract import ScoreComponent


def _band_label(namespace: str, value: float, *, low: str, medium: str, high: str) -> str:
    if value >= 0.75:
        return f"{namespace}.{high}"
    if value >= 0.4:
        return f"{namespace}.{medium}"
    return f"{namespace}.{low}"


def relevance_label(value: float) -> str:
    if value >= 0.75:
        return "relevance.high"
    if value >= 0.25:
        return "relevance.partial"
    return "relevance.none"


def freshness_label(value: float) -> str:
    if value >= 0.75:
        return "freshness.recent"
    if value >= 0.4:
        return "freshness.stable"
    return "freshness.stale"


def confidence_label(value: float) -> str:
    return _band_label("confidence", value, low="low", medium="medium", high="high")


def salience_label(value: float) -> str:
    return _band_label("salience", value, low="low", medium="medium", high="high")


def reuse_label(value: float) -> str:
    return _band_label("reuse", value, low="low", medium="medium", high="high")


def lifecycle_label(tier: str) -> str:
    return f"lifecycle.{str(tier or 'candidate').strip().lower()}"


def provenance_label(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if not normalized:
        return "provenance.unknown"
    if any(marker in normalized for marker in ("confirm", "manual", "user.")):
        return "provenance.user_confirmed"
    if "migration" in normalized or "import" in normalized:
        return "provenance.migration"
    if any(marker in normalized for marker in ("scrape", "paper", "news", "external", "http")):
        return "provenance.external_source"
    if any(marker in normalized for marker in ("tool", "cli", "adapter")):
        return "provenance.tool_generated"
    return "provenance.first_party"


def component_labels(*, components: dict[str, ScoreComponent], source: str, tier: str, memory_type: str, activity: str) -> list[str]:
    labels = [
        relevance_label(components["relevance"].value),
        confidence_label(components["confidence"].value),
        salience_label(components["salience"].value),
        freshness_label(components["freshness"].value),
        reuse_label(components["reuse"].value),
        provenance_label(source),
        lifecycle_label(tier),
        f"memory.{str(memory_type or 'memory').strip().lower() or 'memory'}",
        "risk.none" if components["risk_penalty"].value <= 0.0 else "risk.present",
        "dcmi.type.memory",
        f"dcmi.source.{str(source or 'unknown').strip().lower().replace(' ', '_') or 'unknown'}",
        f"prov.activity.{str(activity or 'memory.score').strip().lower().replace(' ', '_')}",
        "prov.agent.eimemory.scoring.v1",
    ]
    return [label for label in labels if label]
