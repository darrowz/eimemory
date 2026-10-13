"""Pure numeric validation shared by evidence and scoring consumers."""
from __future__ import annotations

from math import isfinite
from typing import Any


def finite_score_number(value: Any) -> float:
    """Parse a finite score number without accepting booleans or containers."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid_numeric_score_value")
    if isinstance(value, str) and not value.strip():
        raise ValueError("invalid_numeric_score_value")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_numeric_score_value") from exc
    if not isfinite(numeric):
        raise ValueError("nonfinite_numeric_score_value")
    return numeric
