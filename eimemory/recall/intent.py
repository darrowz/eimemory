from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_TERM_PATTERN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True)
class RecallIntent:
    name: str
    confidence: float
    reasons: tuple[str, ...]
    preferred_kinds: tuple[str, ...]
    suppressed_kinds: tuple[str, ...]
    source_weights: dict[str, float]
    memory_cube: str
    query_terms: tuple[str, ...]


def classify_recall_intent(query: str, task_context: dict | None = None) -> RecallIntent:
    normalized_query = str(query or "").strip()
    normalized_lower = normalized_query.lower()
    context = dict(task_context or {})
    context_intent = str(context.get("intent") or context.get("task_intent") or "").strip().lower()
    context_task_type = str(context.get("task_type") or "").strip().lower()
    context_query_type = str(context.get("query_type") or "").strip().lower()
    context_hint = " ".join(value for value in (context_intent, context_task_type, context_query_type) if value)
    query_terms = _extract_terms(normalized_lower)

    scores = {
        "project_delivery": 0.0,
        "operator_preference": 0.0,
        "living_posture": 0.0,
        "research": 0.0,
        "news": 0.0,
        "report": 0.0,
        "generic": 0.0,
    }
    reasons: dict[str, list[str]] = {name: [] for name in scores}

    if not normalized_query:
        return RecallIntent(
            name="generic",
            confidence=0.0,
            reasons=("empty_query",),
            preferred_kinds=(),
            suppressed_kinds=(),
            source_weights={},
            memory_cube="general",
            query_terms=(),
        )

    for reason in _report_match_reasons(normalized_query, normalized_lower, context_intent):
        scores["report"] = max(scores["report"], 0.0) + 0.96
        reasons["report"].append(reason)
    _apply_project_delivery_cues(
        normalized_query=normalized_query,
        normalized_lower=normalized_lower,
        query_terms=query_terms,
        scores=scores,
        reasons=reasons,
    )
    _apply_operator_preference_cues(
        normalized_lower=normalized_lower,
        context_hint=context_hint,
        scores=scores,
        reasons=reasons,
    )
    _apply_living_posture_cues(
        normalized_lower=normalized_lower,
        context_hint=context_hint,
        scores=scores,
        reasons=reasons,
    )
    _apply_research_cues(
        normalized_lower=normalized_lower,
        scores=scores,
        reasons=reasons,
    )
    _apply_news_cues(normalized_lower=normalized_lower, scores=scores, reasons=reasons)

    if any(marker in context_hint for marker in ("project_delivery", "project delivery")):
        scores["project_delivery"] += 0.62
        reasons["project_delivery"].append("context: project_delivery")
    if "research" in context_hint:
        scores["research"] += 0.62
        reasons["research"].append("context: research")
    if "news" in context_hint:
        scores["news"] += 0.62
        reasons["news"].append("context: news")
    if "report" in context_hint:
        scores["report"] += 0.62
        reasons["report"].append("context: report")
    if "operator_preference" in context_hint:
        scores["operator_preference"] += 0.62
        reasons["operator_preference"].append("context: operator_preference")
    if "living_posture" in context_hint:
        scores["living_posture"] += 0.62
        reasons["living_posture"].append("context: living_posture")

    intent_name = _pick_intent(scores, reasons)
    return _build_intent(
        name=intent_name,
        scores=scores,
        reasons=reasons,
        query_terms=query_terms,
    )


def recall_filters_for_intent(intent: RecallIntent) -> dict[str, Any]:
    return {
        "intent": intent.name,
        "memory_cube": intent.memory_cube,
        "preferred_kinds": intent.preferred_kinds,
        "suppressed_kinds": intent.suppressed_kinds,
        "source_weights": dict(intent.source_weights),
        "query_terms": intent.query_terms,
        "reasons": intent.reasons,
        "query": " ".join(intent.query_terms),
        "living_query_terms": intent.query_terms,
    }


def _report_match_reasons(
    query: str,
    query_lower: str,
    context_intent: str,
) -> list[str]:
    reasons: list[str] = []
    if re.search(r"rule_evolution_[0-9a-z]+_[0-9a-f]+", query_lower):
        reasons.append("pattern: rule_evolution_id")
    if any(marker in query_lower for marker in ("rule evolution", "rule_evolution", "进化报告", "治理报告")):
        reasons.append("keyword: report")
    if context_intent == "report":
        reasons.append("context: report")
    return reasons


def _apply_project_delivery_cues(
    *,
    normalized_query: str,
    normalized_lower: str,
    query_terms: tuple[str, ...],
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> None:
    if re.search(r"\buumit\b", normalized_lower):
        scores["project_delivery"] += 0.6
        reasons["project_delivery"].append("pattern: project_code")
    if any(marker in normalized_lower for marker in ("交付", "delivery", "delivery quality", "品质", "外部订单", "交付要求")):
        scores["project_delivery"] += 0.25
        reasons["project_delivery"].append("keyword: delivery")
    if "uu" in query_terms and any(term in {"mit", "project", "v2"} for term in query_terms):
        scores["project_delivery"] += 0.1
        reasons["project_delivery"].append("token: uu + mit/project/v2")
    if "project_delivery" in normalized_lower:
        scores["project_delivery"] += 0.06
        reasons["project_delivery"].append("keyword: project_delivery")
    if any(marker in normalized_query for marker in ("海报", "要求", "交付")):
        scores["project_delivery"] += 0.1
        reasons["project_delivery"].append("keyword: delivery_term")


def _apply_operator_preference_cues(
    *,
    normalized_lower: str,
    context_hint: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> None:
    if "operator_preference" in context_hint:
        scores["operator_preference"] += 0.6
        reasons["operator_preference"].append("context: operator_preference")
    if any(marker in normalized_lower for marker in ("沟通风格", "communication style", "reply style", "operator", "偏好", "沟通 方式")):
        scores["operator_preference"] += 0.45
        reasons["operator_preference"].append("keyword: operator_preference")
    if "鸿哥" in normalized_lower:
        scores["operator_preference"] += 0.35
        reasons["operator_preference"].append("keyword: hongge")


def _apply_living_posture_cues(
    *,
    normalized_lower: str,
    context_hint: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> None:
    if "living_posture" in context_hint:
        scores["living_posture"] += 0.6
        reasons["living_posture"].append("context: living_posture")
    if any(marker in normalized_lower for marker in ("姿态", "posture", "act", "nudge", "let go", "repair before", "let_go", "letgo")):
        scores["living_posture"] += 0.28
        reasons["living_posture"].append("keyword: living_posture")


def _apply_research_cues(
    *,
    normalized_lower: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> None:
    if any(marker in normalized_lower for marker in ("graphiti", "arxiv", "论文", "knowledge graph", "paper", "benchmark", "研究", "research")):
        scores["research"] += 0.8
        reasons["research"].append("keyword: research")


def _apply_news_cues(
    *,
    normalized_lower: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> None:
    if "新闻" in normalized_lower:
        scores["news"] += 0.82
        reasons["news"].append("keyword: 新闻")
    if "news" in normalized_lower and any(word in normalized_lower for word in ("ai", "今日", "today", "今天", "最新", "要闻")):
        scores["news"] += 0.45
        reasons["news"].append("keyword: news")


def _pick_intent(scores: dict[str, float], reasons: dict[str, list[str]]) -> str:
    ranked = sorted(
        ((name, score) for name, score in scores.items()),
        key=lambda item: (item[1], _intent_rank(item[0])),
        reverse=True,
    )
    top_name, top_score = ranked[0]
    if top_score <= 0.0:
        return "generic"
    if top_name == "generic" and reasons[top_name]:
        return "generic"
    if not reasons[top_name]:
        return "generic"
    return top_name


def _intent_rank(name: str) -> int:
    order = (
        "project_delivery",
        "operator_preference",
        "living_posture",
        "research",
        "news",
        "report",
        "generic",
    )
    return len(order) - order.index(name)


def _build_intent(
    *,
    name: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
    query_terms: tuple[str, ...],
) -> RecallIntent:
    config = _INTENT_CONFIG[name]
    confidence = round(min(1.0, scores[name]), 3)
    ordered_reasons = tuple(dict.fromkeys(reasons[name]))
    return RecallIntent(
        name=name,
        confidence=confidence,
        reasons=ordered_reasons,
        preferred_kinds=config["preferred_kinds"],
        suppressed_kinds=config["suppressed_kinds"],
        source_weights=dict(config["source_weights"]),
        memory_cube=config["memory_cube"],
        query_terms=query_terms,
    )


def _extract_terms(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in _TERM_PATTERN.findall(text):
        lower_term = term.casefold().strip()
        if len(lower_term) < 2:
            continue
        if lower_term in seen:
            continue
        seen.add(lower_term)
        terms.append(lower_term)
    return tuple(terms)


_INTENT_CONFIG = {
    "project_delivery": {
        "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
        "suppressed_kinds": ("knowledge_page",),
        "source_weights": {
            "eibrain.policy": 1.2,
            "eimemory.knowledge.compiler": 1.1,
            "eimemory.knowledge.synthesis": 1.0,
        },
        "memory_cube": "project",
    },
    "operator_preference": {
        "preferred_kinds": ("memory", "rule", "reflection"),
        "suppressed_kinds": ("knowledge_page",),
        "source_weights": {
            "eibrain.policy": 1.3,
            "eimemory.knowledge.claims": 1.1,
            "operator.correction": 1.0,
        },
        "memory_cube": "operator",
    },
    "living_posture": {
        "preferred_kinds": ("memory", "rule", "reflection"),
        "suppressed_kinds": ("knowledge_page",),
        "source_weights": {
            "eimemory.living": 1.2,
            "eibrain.policy": 1.0,
        },
        "memory_cube": "project",
    },
    "research": {
        "preferred_kinds": ("claim_card", "knowledge_page", "memory", "reflection"),
        "suppressed_kinds": (),
        "source_weights": {
            "eimemory.knowledge.compiler": 1.4,
            "eimemory.knowledge.synthesis": 1.2,
        },
        "memory_cube": "research",
    },
    "news": {
        "preferred_kinds": ("news", "knowledge_page", "memory", "claim_card"),
        "suppressed_kinds": (),
        "source_weights": {
            "eimemory.news.collect": 1.5,
            "eimemory.news.digest": 1.2,
            "eimemory.intake.collect": 1.0,
        },
        "memory_cube": "news",
    },
    "report": {
        "preferred_kinds": ("reflection",),
        "suppressed_kinds": (),
        "source_weights": {
            "eimemory.rule_evolution_loop": 1.8,
            "eimemory.knowledge.claims": 1.0,
        },
        "memory_cube": "governance",
    },
    "generic": {
        "preferred_kinds": (),
        "suppressed_kinds": (),
        "source_weights": {},
        "memory_cube": "general",
    },
}
