"""Harness Patch — versioned asset for governance evolution.

Implements design principles in
``docs/superpowers/specs/2026-06-22-harness-patch-design-principles.md``.

Every governance candidate promotion MUST carry a ``ProposalCard``. The card
is enforced at:

* ``capability_ledger.record_capability_score`` (rejects writes missing the card)
* ``candidate_search.generate_candidate_policies`` (rejects oversized / non-diverse groups)
* ``regression_watch.evaluate_harness_gate`` (requires held-in ∩ held-out ∩ delta≥0)
* ``promotion_manager.promote_candidate`` (rejects L3+ without safety wire)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


# Backwards-compat: opt-in via env var so 1.5.x callers are unaffected.
#
# NOTE: this constant is captured at module-import time. Tests and runtime code
# that need to react to env-var changes after import must use ``_is_v2_enabled()``
# below instead of importing this name.
HARNESS_PATCH_V2 = os.environ.get("HARNESS_PATCH_V2") == "1"


def _is_v2_enabled() -> bool:
    """Runtime check for the HARNESS_PATCH_V2 opt-in env var.

    Always reads ``os.environ`` at call time, so monkeypatching the env var in
    tests (or flipping it in a running process) takes effect immediately. Use
    this helper instead of importing ``HARNESS_PATCH_V2`` whenever the decision
    needs to reflect the *current* process state.
    """
    return os.environ.get("HARNESS_PATCH_V2") == "1"


class HarnessSurface(str, Enum):
    """The five editable surfaces of an agent harness."""

    INSTRUCTION = "INSTRUCTION"
    VERIFICATION_GUIDANCE = "VERIFICATION_GUIDANCE"
    TOOL_LOOP_GUARD = "TOOL_LOOP_GUARD"
    ARTIFACT_RECOVERY = "ARTIFACT_RECOVERY"
    RUNTIME_POLICY = "RUNTIME_POLICY"


@dataclass(frozen=True, slots=True)
class ProposalCard:
    """Mandatory metadata for any harness candidate promotion.

    All required fields are positional-by-keyword and have no default; the
    optional fields (``notes``, ``safety_wire``) default to safe empty values.
    Any instantiation missing a required field raises TypeError — the desired
    enforcement at construction time.
    """

    target_surface: HarnessSurface
    evidence_record_ids: tuple[str, ...]
    expected_delta: float
    target_agent: str  # e.g. "eibrain", "openclaw", "mcp_consumer"
    risk_tier: str  # "L0" | "L1" | "L2" | "L3" | "L4"
    rollback_plan: str
    diff_lines: int
    diff_tokens: int
    notes: str = ""
    safety_wire: tuple[str, ...] = field(default_factory=tuple)  # required for L3+


class GateVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    WARN = "WARN"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of a single ``HarnessGate.evaluate`` call."""

    verdict: GateVerdict
    reason: str
    held_in_score: float | None
    held_out_score: float | None
    delta: float | None


@dataclass(slots=True)
class HarnessGate:
    """Dual-regression gate: held-in ∩ held-out ∩ delta≥0.

    Acceptance rules:
        held_in delta < 0     -> REJECT
        held_out delta < 0    -> REJECT
        held_out missing      -> WARN when ``allow_warn_on_missing`` (default),
                                 else REJECT
        both splits up        -> ACCEPT
    """

    card: ProposalCard
    allow_warn_on_missing: bool = True

    def evaluate(
        self,
        *,
        held_in_scores: dict[str, float],
        held_out_scores: dict[str, float] | None,
        baseline_held_in: float,
        baseline_held_out: float | None,
    ) -> GateResult:
        held_in_now = float(held_in_scores.get("accuracy") or 0.0)
        held_in_delta = held_in_now - float(baseline_held_in)

        if held_in_delta < 0:
            return GateResult(
                verdict=GateVerdict.REJECT,
                reason=f"held_in regressed: {held_in_delta:.4f}",
                held_in_score=held_in_now,
                held_out_score=None,
                delta=held_in_delta,
            )

        if held_out_scores is None or baseline_held_out is None:
            if self.allow_warn_on_missing:
                return GateResult(
                    verdict=GateVerdict.WARN,
                    reason="held_out split missing — falling back to held-in-only check",
                    held_in_score=held_in_now,
                    held_out_score=None,
                    delta=held_in_delta,
                )
            return GateResult(
                verdict=GateVerdict.REJECT,
                reason="held_out split required but missing",
                held_in_score=held_in_now,
                held_out_score=None,
                delta=held_in_delta,
            )

        held_out_now = float(held_out_scores.get("accuracy") or 0.0)
        held_out_delta = held_out_now - float(baseline_held_out)
        if held_out_delta < 0:
            return GateResult(
                verdict=GateVerdict.REJECT,
                reason=f"held_out regressed: {held_out_delta:.4f}",
                held_in_score=held_in_now,
                held_out_score=held_out_now,
                delta=min(held_in_delta, held_out_delta),
            )

        min_delta = min(held_in_delta, held_out_delta)
        return GateResult(
            verdict=GateVerdict.ACCEPT,
            reason=f"held_in={held_in_delta:.4f} held_out={held_out_delta:.4f}",
            held_in_score=held_in_now,
            held_out_score=held_out_now,
            delta=min_delta,
        )


__all__ = [
    "HARNESS_PATCH_V2",
    "HarnessSurface",
    "ProposalCard",
    "GateVerdict",
    "GateResult",
    "HarnessGate",
    "_is_v2_enabled",
]
