"""Prompt-safety checks for the L2 promotion gate.

This module is consumed by ``eimemory.governance.autonomous_learning`` to
populate the ``prompt_shadow_eval`` and ``prompt_injection_check`` fields of
the L2 gate bundle.

The two exported check functions are **stubs** — they do meaningful work
(regex-scan a candidate prompt for known injection markers) so that the
L2 gate cannot silently return ``passed=True`` without exercising real
inspection of the input, but they are not a complete adversarial battery
and are explicitly flagged with :data:`PROMPT_SAFETY_STUB_NOTREADY`.

TODO(R10): Replace the canned regex battery with a real check suite:

* ``prompt_shadow_eval`` should run the candidate prompt through the LLM in
  shadow mode and assert the output stays within expected behavioural bounds
  (no role-switch, no tool exfiltration, no policy bypass).
* ``prompt_injection_check`` should run an adversarial case battery
  (jailbreak strings, indirect-prompt-injection payloads, role-override
  attempts) and verify the LLM does not break character on any of them.

Until then, callers MUST surface :data:`PROMPT_SAFETY_STUB_NOTREADY` in the
gate bundle so the downstream promotion manager and reviewers can see the
check is not full-strength.
"""
from __future__ import annotations

import re
from typing import Final

# Public flag: ``True`` while the real check suite is unimplemented. The L2
# gate bundle embeds this value as ``notready`` so downstream code can act
# accordingly (e.g. require an extra human review step).
PROMPT_SAFETY_STUB_NOTREADY: Final[bool] = True


# A small canned battery of common injection markers. This is intentionally
# conservative — false positives (rejecting a clean prompt) are acceptable
# because they just route the candidate to a human review; false negatives
# are the failure mode the L2 gate is trying to catch.
_INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior|above|earlier)", re.IGNORECASE),
    re.compile(r"forget\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|rules?)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"pretend\s+(?:to\s+be|you\s+are)\s+", re.IGNORECASE),
    re.compile(r"\bsystem\s*(?:prompt|rules?)\s*:\s*", re.IGNORECASE),
    re.compile(r"</?\s*(?:system|assistant|user|tool|developer)\s*>", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"\bDAN\b\s+mode", re.IGNORECASE),
    re.compile(r"reveal\s+(?:your|the)\s+(?:system|hidden|secret)\s+prompt", re.IGNORECASE),
    re.compile(r"print\s+(?:your|the)\s+(?:system|initial)\s+prompt", re.IGNORECASE),
    re.compile(r"override\s+(?:safety|guardrails?|filters?)", re.IGNORECASE),
)


def _scan_injection_markers(prompt: str) -> bool:
    """Return ``True`` when ``prompt`` looks injection-free.

    Empty / non-string inputs are treated as safe (no markers to match) so
    that callers passing a missing candidate still get a deterministic
    answer. Real callers in this module always coerce the input to ``str``
    before reaching this helper.
    """
    if not prompt:
        return True
    return not any(pattern.search(prompt) for pattern in _INJECTION_PATTERNS)


def _coerce_cases(cases: object) -> int:
    """Return a positive integer case count, defaulting to 1 on bad input.

    The stub does not actually run a battery of ``cases`` items; it only
    surfaces the requested count in the gate bundle so the bundle shape
    matches the eventual real implementation.
    """
    if isinstance(cases, bool):
        # ``bool`` is a subclass of ``int``; explicitly reject so True/False
        # never silently becomes 1/0 case count.
        return 1
    if isinstance(cases, int) and cases >= 1:
        return int(cases)
    return 1


def prompt_shadow_eval(prompt: str, cases: int = 3) -> bool:
    """Shadow-evaluate a candidate system prompt.

    TODO(R10): Replace with a real shadow eval that actually runs the
    candidate prompt through the LLM and inspects the output for
    role-switch, tool-exfiltration, and policy-bypass behaviour. The stub
    here runs the canned injection-marker regex battery against ``prompt``
    and returns ``True`` only when no marker matches.

    Args:
        prompt: Candidate system prompt text. May be empty; the empty string
            is treated as safe.
        cases: Number of canned cases the (real) battery would run. The
            stub does not consume this beyond type validation — it is
            surfaced in the gate bundle for the eventual real impl.

    Returns:
        ``True`` when no injection marker matches, ``False`` otherwise.
    """
    _coerce_cases(cases)
    return _scan_injection_markers(str(prompt or ""))


def prompt_injection_check(prompt: str, cases: int = 3) -> bool:
    """Run a canned prompt-injection battery against ``prompt``.

    TODO(R10): Replace with a real adversarial injection battery that
    exercises the LLM with both clean and adversarial cases and checks the
    output stays within expected bounds. The stub here shares the
    injection-marker regex battery with :func:`prompt_shadow_eval` and
    returns the same result — splitting shadow vs injection is a v2
    concern once the real LLM-backed checks exist.

    Args:
        prompt: Candidate system prompt text. May be empty.
        cases: Number of canned cases the (real) battery would run.

    Returns:
        ``True`` when no injection marker matches, ``False`` otherwise.
    """
    _coerce_cases(cases)
    return _scan_injection_markers(str(prompt or ""))
