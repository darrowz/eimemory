"""Separate replay evidence from verified production outcomes.

Classification describes provenance; capability contracts still need their own
validation. A caller selecting capability traces must require host provenance.
"""
import re
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
        verified = verifier.get("passed") is True or _verified_failure(payload, verifier)
    else:
        verified = bool(payload.get("verification")) and host
        # Existing event adapters treat explicit corrections as safety
        # evidence. This exception never supplies capability trace authority.
        if (not require_host and str(payload.get("outcome") or "").lower() == "bad"
                and bool(str(payload.get("correction_from_user") or "").strip())):
            verified = True
    eligible = verified and (not require_host or (host and explicit_real))
    return {"evidence_class": "verified_real_task" if eligible else "unverified", "production_eligible": eligible}


def _verified_failure(payload: dict[str, Any], verifier: dict[str, Any]) -> bool:
    """A failed assertion can be evidence when its execution is identified.

    Host trace adapters emit method/evidence/checks even when a task fails.
    A bare false flag also describes missing or unexecuted verification, so it
    must not grant observation authority on its own.
    """
    outcome = payload.get("outcome")
    status = outcome.get("status") if isinstance(outcome, dict) else outcome
    if verifier.get("passed") is not False or str(status or "").lower() not in {"bad", "failed", "failure", "error"}:
        return False
    attribution = payload.get("capability_attribution")
    refs = verifier.get("evidence_refs") or (attribution.get("evidence_refs") if isinstance(attribution, dict) else None)
    if not isinstance(refs, (list, tuple)) or not any(str(ref).strip() for ref in refs):
        return False
    source = str(payload.get("source") or "")
    checks = verifier.get("checks")
    if isinstance(checks, dict) and any(
        _unexecuted_verification_state(checks.get(field)) for field in ("verification", "result")
    ):
        return False
    if source in _HOST_SOURCES and verifier.get("method") == source and isinstance(checks, dict):
        verification = str(checks.get("verification") or "").strip().lower()
        if verification and verification not in {"absent", "error"}:
            return True
    digest = str(verifier.get("contract_digest") or "").lower()
    return bool(
        verifier.get("independent") is True
        and str(verifier.get("id") or "").strip()
        and str(verifier.get("revision") or "").strip()
        and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    )


def _unexecuted_verification_state(value: Any) -> bool:
    # Match the host adapter's status grammar without importing the adapter
    # back into governance (which would create a runtime dependency cycle).
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).split())
    prefixes = ("not run", "not executed", "skipped", "skip", "unavailable", "unknown", "missing", "uncertain")
    return any(normalized == prefix or normalized.startswith(prefix + " ") for prefix in prefixes)
