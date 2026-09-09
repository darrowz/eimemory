"""Separate replay evidence from verified production outcomes.

Classification describes provenance; capability contracts still need their own
validation. A caller selecting capability traces must require host provenance.
"""
from typing import Any


_HOST_SOURCES = frozenset({"openclaw.agent_end", "openclaw.task_end", "codex.stop", "hermes.task_end"})
_REPLAY_MARKERS = ("replay", "rehearsal", "eval_probe", "acceptance_probe")


def outcome_evidence(payload: dict[str, Any], *, require_host: bool = False) -> dict[str, Any]:
    def replay(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("rehearsal") is True or value.get("probe") is True:
                return True
            for key in ("source", "evidence_class", "evidence_type"):
                if any(marker in str(value.get(key) or "").lower() for marker in _REPLAY_MARKERS):
                    return True
            return any(replay(child) for child in value.values())
        return isinstance(value, list) and any(replay(child) for child in value)

    if replay(payload):
        return {"evidence_class": "replay", "production_eligible": False}
    nested = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    host = str(payload.get("source") or "") in _HOST_SOURCES
    explicit_real = payload.get("rehearsal") is False or nested.get("rehearsal") is False
    verifier = payload.get("verifier")
    if isinstance(verifier, dict):
        verified = verifier.get("passed") is True
    else:
        verified = bool(payload.get("verification")) and host
        # Existing event adapters treat explicit corrections as safety
        # evidence. This exception never supplies capability trace authority.
        if (not require_host and str(payload.get("outcome") or "").lower() == "bad"
                and bool(str(payload.get("correction_from_user") or "").strip())):
            verified = True
    eligible = verified and (not require_host or (host and explicit_real))
    return {"evidence_class": "verified_real_task" if eligible else "unverified", "production_eligible": eligible}
