"""Finite preference observations; no query, context or candidate text."""
from __future__ import annotations

from math import isfinite

PREFERENCE_ROUTE_SCHEMA = "preference_query_route.v1"
ROUTE_ENTRIES = frozenset({"typed_request", "custom_marker", "explicit_context", "research_exempt", "default_marker",
                           "intent_fallback", "no_match", "unobserved"})
BUILTIN_MARKERS = frozenset({"preference", "reply style", "communication style", "operator preference",
                             "user preference", "偏好", "喜欢", "讨厌", "沟通风格", "回复风格"})
INTENT_NAMES = frozenset({"task_recall", "project_delivery", "operator_preference", "living_posture",
                          "operational_issue", "research", "news", "report", "generic"})
CANDIDATE_REJECTIONS = frozenset({"not_memory", "recall_diagnostic", "no_explicit_preference",
                                  "requested_assertion_not_proven", "unobserved"})


def _code(value, allowed):
    if value is None:
        return "not_reported"
    if isinstance(value, str) and value in {"not_reported", "not_allowlisted"}:
        return value
    return value if isinstance(value, str) and value in allowed else "not_allowlisted"


def _bool(value):
    return value if type(value) is bool else None


def safe_preference_route(value):
    if not isinstance(value, dict) or value.get("schema") != PREFERENCE_ROUTE_SCHEMA:
        return {"status": "not_reported"}
    confidence = value.get("intent_confidence")
    return {
        "schema": PREFERENCE_ROUTE_SCHEMA,
        "entry": _code(value.get("entry"), ROUTE_ENTRIES),
        "selected": _bool(value.get("selected")),
        "filter_applied": _bool(value.get("filter_applied")),
        "report_bypassed": _bool(value.get("report_bypassed")),
        "builtin_marker": _code(value.get("builtin_marker"), BUILTIN_MARKERS | {""}),
        "custom_marker_matched": _bool(value.get("custom_marker_matched")),
        "typed_mode": _code(value.get("typed_mode"), {"", "response_style", "list_preferences"}),
        "typed_attribute": _code(value.get("typed_attribute"), {"", "language", "length", "format", "tone"}),
        "intent_name": _code(value.get("intent_name"), INTENT_NAMES),
        "intent_confidence": (confidence if type(confidence) in (int, float)
                              and 0 <= confidence <= 1 and isfinite(confidence) else None),
        "intent_topic_match": _bool(value.get("intent_topic_match")),
    }


def safe_preference_rejections(value):
    if not isinstance(value, list):
        return []
    return [{"branch": _code(item.get("branch"), {"preference_query", "preference_attribute"}),
             "reason": _code(item.get("reason"), CANDIDATE_REJECTIONS)}
            for item in value[:2] if isinstance(item, dict)]
