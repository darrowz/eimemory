from __future__ import annotations

from typing import Any


TRUST_CATEGORIES = {
    "user_explicit",
    "system_verified",
    "trusted_hook",
    "agent_inferred",
    "web_external",
    "unknown",
}

TRUSTED_HOOK_SOURCES = {
    "openclaw.message_received",
    "openclaw.before_prompt_build",
    "openclaw.agent_end",
    "openclaw.terminal",
    "openclaw.hooks",
}

_VERIFICATION_HINT_KEYS = {
    "verification",
    "health_check",
    "health",
    "tests",
    "test_results",
    "test_report",
    "verification_method",
}


def classify_outcome_source(
    *,
    outcome: dict[str, Any],
    event: dict[str, Any] | None = None,
    source: str | None = None,
) -> str:
    """Classify an outcome into one of the trust categories.

    The classifier prefers explicit `source_trust` values when provided and then
    falls back to intent signals from existing payload fields.
    """

    explicit = _coerce_text(outcome.get("source_trust") or outcome.get("trust") or "")
    if explicit in TRUST_CATEGORIES:
        return explicit

    event_source = _coerce_text(outcome.get("source") or source or "")
    if event_source == "web_hypothesis" or event_source.startswith("web"):
        return "web_external"

    correction = _coerce_text(outcome.get("correction_from_user"))
    if correction:
        return "user_explicit"

    if _has_system_verification(outcome=outcome, event=event):
        return "system_verified"

    if event_source in TRUSTED_HOOK_SOURCES:
        return "trusted_hook"

    if event_source:
        # Outcomes from hooks already validated by core control flow default to agent inference.
        if event_source.startswith("openclaw.") and not correction:
            return "agent_inferred"

    # keep explicit source values unknown for future upstream integrations.
    return "agent_inferred"


def evaluate_trust_gate(
    *,
    outcome: dict[str, Any],
    event: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Evaluate whether the origin trust of an outcome can auto-apply.

    Returns a structured gate report used by autonomous-evolution patch decisions.
    """

    source_trust = classify_outcome_source(outcome=outcome, event=event, source=source)
    if source_trust in {"user_explicit", "system_verified"}:
        return {
            "ok": True,
            "source_trust": source_trust,
            "allow": True,
            "reasons": [],
        }

    if source_trust == "trusted_hook":
        trust_strength = _coerce_text(outcome.get("trusted_hook_strength") or outcome.get("hook_strength") or "")
        if trust_strength == "strong":
            return {
                "ok": True,
                "source_trust": source_trust,
                "allow": True,
                "reasons": ["trusted_hook_strength:strong"],
            }
        return {
            "ok": False,
            "source_trust": source_trust,
            "allow": False,
            "reasons": ["trusted_hook_strength_not_strong"],
        }

    if source_trust == "web_external":
        return {
            "ok": False,
            "source_trust": source_trust,
            "allow": False,
            "reasons": ["web_hypothesis_replay_only"],
        }

    return {
        "ok": False,
        "source_trust": source_trust,
        "allow": False,
        "reasons": [f"trusted_gate_reject:{source_trust}"],
    }


def _has_system_verification(
    *,
    outcome: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> bool:
    if any(_coerce_text(outcome.get(key)) for key in _VERIFICATION_HINT_KEYS):
        return True
    if event is not None:
        return any(_coerce_text(event.get(key)) for key in _VERIFICATION_HINT_KEYS)
    return False


def _coerce_text(value: Any) -> str:
    text = str(value or "").strip()
    return text
