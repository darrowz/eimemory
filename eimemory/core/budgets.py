"""Single source for the recall budget and the client timeout derived from it.

INT-3: an adapter client waits for the server budget plus a margin, so a
client timeout cannot land while the server is still committing that call.
"""

from __future__ import annotations

import os


RECALL_BUDGET_SECONDS = 3.0
ADAPTER_TIMEOUT_MARGIN_SECONDS = 0.5
SHORT_LIFECYCLE_TIMEOUT_SECONDS = 0.8


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
