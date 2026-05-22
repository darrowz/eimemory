from __future__ import annotations

import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.metadata import business_metadata
from eimemory.scoring.contract import MemoryScore, ScoreComponent, ScoreContext, ScoreProvenance
from eimemory.scoring.labels import component_labels, provenance_label
from eimemory.scoring.thresholds import clamp_score, tier_for_score, weights_for_profile


HIGH_VALUE_KEYWORDS = {
    "always",
    "decision",
    "decided",
    "important",
    "must",
    "never",
    "prefer",
    "preference",
    "project",
    "remember",
    "rule",
    "should",
    "记住",
    "偏好",
    "决策",
    "规则",
    "项目",
    "重要",
}
UNCERTAIN_KEYWORDS = {"maybe", "perhaps", "guess", "unsure", "可能", "也许", "不确定"}
REUSABLE_KEYWORDS = {
    "api",
    "architecture",
    "config",
    "contract",
    "deploy",
    "eibrain",
    "eimemory",
    "interface",
    "openclaw",
    "policy",
    "scope",
    "server",
    "tenant",
    "user",
}
INJECTION_MARKERS = {
    "ignore previous instructions",
    "system prompt",
    "developer message",
    "jailbreak",
}


def _normalized_terms(text: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[\w]+", text, flags=re.UNICODE) if term.strip()]


def _provenance_score(source: str) -> float:
    label = provenance_label(source)
    if label == "provenance.user_confirmed":
        return 0.95
    if label == "provenance.first_party":
        return 0.8
    if label == "provenance.tool_generated":
        return 0.72
    if label == "provenance.migration":
        return 0.58
    if label == "provenance.external_source":
        return 0.46
    return 0.3


def _score_activity(context: ScoreContext) -> str:
    activity = str(context.activity or "").strip().lower()
    if "recall" in activity:
        return "memory.recall_score"
    if "repair" in activity or "backfill" in activity or "quality" in activity:
        return "memory.backfill_score"
    return "memory.score"


def _risk_flags(*, combined: str, text: str, thin_or_noisy: bool, source: str, legacy_quality: dict[str, Any]) -> tuple[float, list[str], dict[str, Any]]:
    normalized = combined.lower()
    risk = 0.0
    labels: list[str] = []
    evidence: dict[str, Any] = {}
    if thin_or_noisy:
        risk = max(risk, 0.85)
        evidence["thin_or_noisy"] = True
    if "\ufffd" in combined:
        risk = max(risk, 0.7)
        labels.append("risk.malformed")
    if any(marker in normalized for marker in INJECTION_MARKERS):
        risk = max(risk, 0.85)
        labels.append("risk.injection_suspected")
    if not str(source or "").strip():
        risk = max(risk, 0.45)
        labels.append("risk.unknown_source")
    if legacy_quality.get("capture_decision") == "reject":
        risk = max(risk, 0.75)
        evidence["legacy_reject"] = True
    if "duplicate_of" in legacy_quality:
        risk = max(risk, 0.65)
        labels.append("risk.duplicate")
    if legacy_quality.get("contradicted"):
        risk = max(risk, 0.7)
        labels.append("risk.conflict")
    if not labels and risk <= 0.0:
        labels.append("risk.none")
    evidence["labels"] = labels
    evidence["body_length"] = len(text)
    return clamp_score(risk), labels, evidence


def _capture_components(
    *,
    text: str,
    title: str,
    memory_type: str,
    source: str,
    force_capture: bool,
    legacy_quality: dict[str, Any] | None,
    weights: dict[str, float],
) -> tuple[dict[str, ScoreComponent], list[str]]:
    legacy_quality = dict(legacy_quality or {})
    combined = " ".join(part.strip() for part in (title, text) if part and part.strip())
    terms = _normalized_terms(combined)
    body_terms = _normalized_terms(text)
    normalized = " ".join(terms)
    body_alnum_count = sum(1 for char in text if char.isalnum())
    alnum_count = sum(1 for char in combined if char.isalnum())
    unique_body_terms = len(set(body_terms))
    memory_type = memory_type.lower().strip()
    source = source.lower().strip()

    keyword_hits = sum(1 for keyword in HIGH_VALUE_KEYWORDS if keyword in normalized or keyword in combined)
    reusable_hits = sum(1 for keyword in REUSABLE_KEYWORDS if keyword in normalized or keyword in combined.lower())
    uncertain_hits = sum(1 for keyword in UNCERTAIN_KEYWORDS if keyword in normalized or keyword in combined)

    thin_or_noisy = body_alnum_count < 8 or (len(body_terms) <= 2 and body_alnum_count < 20) or unique_body_terms <= 1
    type_bonus = {
        "decision": 0.24,
        "preference": 0.22,
        "rule": 0.2,
        "fact": 0.14,
        "project": 0.14,
        "conversation": 0.02,
    }.get(memory_type, 0.08)
    source_bonus = 0.08 if any(marker in source for marker in ("tool.store", "migration", "cli")) else 0.0
    length_bonus = min(0.18, alnum_count / 420)

    importance = clamp_score(0.28 + type_bonus + min(0.28, keyword_hits * 0.09) + length_bonus)
    confidence = clamp_score(0.62 + source_bonus - min(0.24, uncertain_hits * 0.08))
    freshness = clamp_score(float(legacy_quality.get("freshness") or 1.0))
    reuse = clamp_score(0.3 + min(0.28, reusable_hits * 0.07) + min(0.18, keyword_hits * 0.045) + type_bonus / 2)
    salience = clamp_score((importance * 0.38) + (confidence * 0.22) + (freshness * 0.12) + (reuse * 0.28))
    relevance = clamp_score(0.18 + min(0.34, unique_body_terms / 18) + min(0.28, body_alnum_count / 160) + min(0.12, keyword_hits * 0.03))
    provenance = clamp_score(_provenance_score(source))

    if legacy_quality:
        confidence = clamp_score(legacy_quality.get("confidence") or confidence)
        freshness = clamp_score(legacy_quality.get("freshness") or freshness)
        reuse = clamp_score(legacy_quality.get("reuse_potential") or reuse)
        salience = clamp_score(legacy_quality.get("salience_score") or salience)
        importance = clamp_score(legacy_quality.get("importance") or salience)

    risk_value, risk_labels, risk_evidence = _risk_flags(
        combined=combined,
        text=text,
        thin_or_noisy=thin_or_noisy and not force_capture,
        source=source,
        legacy_quality=legacy_quality,
    )
    if force_capture and risk_value >= 0.75:
        risk_value = clamp_score(risk_value - 0.4)
        risk_evidence["force_capture"] = True

    components = {
        "relevance": ScoreComponent(
            name="relevance",
            value=relevance,
            weight=weights["relevance"],
            evidence={"specificity_terms": unique_body_terms, "keyword_hits": keyword_hits},
        ),
        "confidence": ScoreComponent(
            name="confidence",
            value=confidence,
            weight=weights["confidence"],
            evidence={"uncertain_hits": uncertain_hits},
        ),
        "salience": ScoreComponent(
            name="salience",
            value=salience,
            weight=weights["salience"],
            evidence={"importance": importance, "keyword_hits": keyword_hits, "memory_type": memory_type},
        ),
        "freshness": ScoreComponent(
            name="freshness",
            value=freshness,
            weight=weights["freshness"],
            evidence={"mode": "capture_default"},
        ),
        "provenance": ScoreComponent(
            name="provenance",
            value=provenance,
            weight=weights["provenance"],
            evidence={"source": source or "unknown", "label": provenance_label(source)},
        ),
        "reuse": ScoreComponent(
            name="reuse",
            value=reuse,
            weight=weights["reuse"],
            evidence={"reusable_hits": reusable_hits, "memory_type": memory_type},
        ),
        "risk_penalty": ScoreComponent(
            name="risk_penalty",
            value=risk_value,
            weight=weights["risk_penalty"],
            evidence=risk_evidence,
        ),
    }
    return components, risk_labels


def evaluate_memory_score(
    *,
    text: str,
    title: str = "",
    memory_type: str = "",
    source: str = "",
    force_capture: bool = False,
    context: ScoreContext | None = None,
    legacy_quality: dict[str, Any] | None = None,
) -> MemoryScore:
    context = context or ScoreContext()
    weights = weights_for_profile(context.profile)
    components, risk_labels = _capture_components(
        text=text,
        title=title,
        memory_type=memory_type,
        source=source or context.source,
        force_capture=force_capture or context.force_capture,
        legacy_quality=legacy_quality,
        weights=weights,
    )
    base_score = sum(
        components[name].value * weights[name]
        for name in ("relevance", "confidence", "salience", "freshness", "provenance", "reuse")
    )
    final_score = clamp_score(base_score - (components["risk_penalty"].value * weights["risk_penalty"]))
    if components["risk_penalty"].evidence.get("thin_or_noisy") and not (force_capture or context.force_capture):
        final_score = min(final_score, 0.24)
    tier = tier_for_score(final_score)
    labels = component_labels(
        components=components,
        source=source or context.source,
        tier=tier,
        memory_type=memory_type,
        activity=_score_activity(context),
    )
    labels.extend(label for label in risk_labels if label not in labels)
    provenance = ScoreProvenance(
        entity_id=context.entity_id,
        activity=_score_activity(context),
        agent="eimemory.scoring.v1",
        source=str(context.source or source or context.activity or "record.create"),
        generated_at=now_iso(),
        inputs=[
            {"title": title},
            {"memory_type": memory_type},
            {"force_capture": bool(force_capture or context.force_capture)},
            *[dict(item) for item in context.inputs],
        ],
    )
    explanation = {
        "profile": str(context.profile or "balanced"),
        "formula": {
            "base_score": round(base_score, 4),
            "risk_multiplier": weights["risk_penalty"],
            "final_score": final_score,
        },
        "capture_decision": "reject" if tier == "rejected" else "accept",
        "risk_labels": risk_labels,
    }
    return MemoryScore(
        final_score=final_score,
        tier=tier,
        components=components,
        labels=labels,
        explanation=explanation,
        provenance=provenance,
    )


def evaluate_recall_score(
    *,
    record: Any,
    query: str,
    lexical_score: float,
    semantic_score: float,
    vector_score: float,
    source_weight: float = 1.0,
    modality_boost: float = 0.0,
    context: ScoreContext | None = None,
    stored_score: MemoryScore | None = None,
) -> MemoryScore:
    context = context or ScoreContext(activity="sqlite.recall", source="sqlite.recall")
    weights = weights_for_profile(context.profile)
    stored = stored_score or evaluate_memory_score(
        text=str(record.content.get("text") or record.summary or record.detail or record.title),
        title=str(record.title or ""),
        memory_type=str(business_metadata(record.meta).get("memory_type") or record.content.get("memory_type") or ""),
        source=str(record.source or ""),
        context=ScoreContext(activity="record.create", source=str(record.source or "")),
        legacy_quality=dict(business_metadata(record.meta).get("quality") or {}),
    )
    query_tokens = [token for token in _normalized_terms(query) if token]
    lexical_norm = 1.0 if not query_tokens else clamp_score(float(lexical_score) / max(1, len(query_tokens)))
    relevance = clamp_score(
        min(
            1.0,
            ((lexical_norm * 0.45) + (clamp_score(semantic_score) * 0.2) + (clamp_score(vector_score) * 0.25))
            * max(0.5, float(source_weight))
            + max(0.0, float(modality_boost)),
        )
    )
    components = {
        "relevance": ScoreComponent(
            name="relevance",
            value=relevance,
            weight=weights["relevance"],
            evidence={
                "query": query,
                "lexical_score": round(float(lexical_score), 4),
                "lexical_norm": lexical_norm,
                "semantic_score": clamp_score(semantic_score),
                "vector_score": clamp_score(vector_score),
                "source_weight": round(float(source_weight), 4),
                "modality_boost": round(float(modality_boost), 4),
            },
        ),
        "confidence": ScoreComponent("confidence", stored.components["confidence"].value, weights["confidence"], stored.components["confidence"].evidence),
        "salience": ScoreComponent("salience", stored.components["salience"].value, weights["salience"], stored.components["salience"].evidence),
        "freshness": ScoreComponent("freshness", stored.components["freshness"].value, weights["freshness"], stored.components["freshness"].evidence),
        "provenance": ScoreComponent("provenance", stored.components["provenance"].value, weights["provenance"], stored.components["provenance"].evidence),
        "reuse": ScoreComponent("reuse", stored.components["reuse"].value, weights["reuse"], stored.components["reuse"].evidence),
        "risk_penalty": ScoreComponent("risk_penalty", stored.components["risk_penalty"].value, weights["risk_penalty"], stored.components["risk_penalty"].evidence),
    }
    base_score = sum(
        components[name].value * weights[name]
        for name in ("relevance", "confidence", "salience", "freshness", "provenance", "reuse")
    )
    final_score = clamp_score(base_score - (components["risk_penalty"].value * weights["risk_penalty"]))
    tier = tier_for_score(final_score)
    labels = component_labels(
        components=components,
        source=str(record.source or context.source),
        tier=tier,
        memory_type=str(business_metadata(record.meta).get("memory_type") or record.content.get("memory_type") or ""),
        activity=_score_activity(context),
    )
    risk_labels = list(components["risk_penalty"].evidence.get("labels") or [])
    labels.extend(label for label in risk_labels if label not in labels)
    provenance = ScoreProvenance(
        entity_id=str(record.record_id or ""),
        activity=_score_activity(context),
        agent="eimemory.scoring.v1",
        source=str(context.source or "sqlite.recall"),
        generated_at=now_iso(),
        inputs=[
            {"query": query},
            {"record_id": str(record.record_id or "")},
            {"profile": str(context.profile or "balanced")},
        ],
    )
    explanation = {
        "profile": str(context.profile or "balanced"),
        "formula": {
            "base_score": round(base_score, 4),
            "risk_multiplier": weights["risk_penalty"],
            "final_score": final_score,
        },
        "stored_tier": stored.tier,
        "stored_final_score": stored.final_score,
    }
    return MemoryScore(
        final_score=final_score,
        tier=tier,
        components=components,
        labels=labels,
        explanation=explanation,
        provenance=provenance,
    )
