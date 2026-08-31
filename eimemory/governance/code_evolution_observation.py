"""Shared observation schedule contract for code-evolution transactions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


OBSERVATION_HOURS = 48
OBSERVATION_OFFSETS = (0, 15 * 60, 60 * 60, 6 * 60 * 60, 12 * 60 * 60, 24 * 60 * 60, 36 * 60 * 60, 48 * 60 * 60)


def parse_observation_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def observation_phase(start: datetime | None, observed: datetime | None) -> int:
    if start is None or observed is None:
        return -1
    elapsed = max(0, int((observed - start).total_seconds()))
    eligible = [offset for offset in OBSERVATION_OFFSETS if elapsed >= offset]
    return eligible[-1] if eligible else 0


def compact_observation_samples(samples: list[dict[str, Any]], start: datetime | None) -> list[dict[str, Any]]:
    """Keep phase witnesses plus recent health; the full event ledger remains.

    Retaining only the last sixteen timer ticks erases early phases long before
    48h. One witness per phase and the latest eight ticks stay bounded at 16
    while preserving both coverage and consecutive-degradation checks.
    """
    witnesses: dict[int, int] = {}
    for index, sample in enumerate(samples):
        phase = observation_phase(start, parse_observation_time(str(sample.get("observed_at") or "")))
        if phase >= 0:
            witnesses.setdefault(phase, index)
    retained = set(witnesses.values()) | set(range(max(0, len(samples) - 8), len(samples)))
    return [sample for index, sample in enumerate(samples) if index in retained]


__all__ = ["OBSERVATION_HOURS", "OBSERVATION_OFFSETS", "observation_phase", "parse_observation_time", "compact_observation_samples"]
