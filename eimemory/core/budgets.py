"""Single source for the recall budget and the client timeout derived from it.

INT-3: an adapter client waits for the server budget plus a margin, so a
client timeout cannot land while the server is still committing that call.
"""

from __future__ import annotations

import os


RECALL_BUDGET_SECONDS = 3.0
ADAPTER_TIMEOUT_MARGIN_SECONDS = 0.5
SHORT_LIFECYCLE_TIMEOUT_SECONDS = 0.8
# Caller-assisted verification may START inside the recall budget; its model
# call is bounded here, not by the (90s default) model transport timeout.
RECALL_VERIFIER_TIMEOUT_SECONDS = 12.0
MAX_RECALL_VERIFIER_TIMEOUT_SECONDS = 60.0
# Final authority read after a completed verifier call (verification_budget).
FINAL_AUTHORITY_SECONDS = 0.75
# Explicit (tool) recall waits for the server's full completion bound.
EXPLICIT_RECALL_TIMEOUT_SECONDS = 30.0
MAX_EXPLICIT_RECALL_TIMEOUT_SECONDS = 120.0


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def recall_budget_seconds() -> float:
    """Hard ceiling for one recall/search, including caller-assisted verification."""

    return _positive_float("EIMEMORY_RECALL_BUDGET_SECONDS", RECALL_BUDGET_SECONDS)


def adapter_timeout_seconds() -> float:
    """Client transport timeout derived from the server recall budget.

    ``EIMEMORY_ADAPTER_TIMEOUT_SECONDS`` still overrides both ends when an
    operator sets it explicitly.  An invalid override falls back to the
    derived budget-plus-margin value.
    """

    explicit = os.environ.get("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", "").strip()
    derived = recall_budget_seconds() + ADAPTER_TIMEOUT_MARGIN_SECONDS
    if not explicit:
        return derived
    try:
        value = float(explicit)
    except ValueError:
        return derived
    if value <= 0:
        return derived
    return value


def short_lifecycle_timeout_seconds() -> float:
    """Non-recall hook events do not start the recall budget."""

    return min(SHORT_LIFECYCLE_TIMEOUT_SECONDS, adapter_timeout_seconds())


def recall_verifier_timeout_seconds() -> float:
    """Upper bound for one caller-assisted verification model call."""

    value = _positive_float("EIMEMORY_RECALL_VERIFIER_TIMEOUT_SECONDS", RECALL_VERIFIER_TIMEOUT_SECONDS)
    return max(1.0, min(MAX_RECALL_VERIFIER_TIMEOUT_SECONDS, value))


def recall_completion_seconds() -> float:
    """Worst-case server time for one recall: collection + verifier + final read + margin."""

    return (
        recall_budget_seconds()
        + recall_verifier_timeout_seconds()
        + FINAL_AUTHORITY_SECONDS
        + ADAPTER_TIMEOUT_MARGIN_SECONDS
    )


def explicit_recall_timeout_seconds() -> float:
    """Client timeout for an explicit, user/model-requested recall tool call.

    The proactive hot path keeps ``adapter_timeout_seconds``. An explicit tool
    call waits for the server's full completion bound. The server budget may
    be configured only in the RPC process, so the default is a fixed ceiling
    (30s) that covers budget 8s + verifier 12s + margins, and it is never
    shorter than the locally derived completion bound or the adapter timeout.
    """

    derived = max(EXPLICIT_RECALL_TIMEOUT_SECONDS, recall_completion_seconds(), adapter_timeout_seconds())
    value = _positive_float("EIMEMORY_EXPLICIT_RECALL_TIMEOUT_SECONDS", derived)
    return max(1.0, min(MAX_EXPLICIT_RECALL_TIMEOUT_SECONDS, value))
