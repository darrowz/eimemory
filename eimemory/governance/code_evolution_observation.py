"""Shared observation schedule contract for code-evolution transactions."""

from __future__ import annotations

from datetime import datetime, timezone


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


__all__ = ["OBSERVATION_HOURS", "OBSERVATION_OFFSETS", "observation_phase", "parse_observation_time"]
