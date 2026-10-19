"""Shared observation schedule contract for code-evolution transactions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import UUID
from typing import Any


# Fixed 8-hour post-deploy observation window (user policy for 1.13.26+).
OBSERVATION_HOURS = 8
DEFAULT_OBSERVATION_SECONDS = OBSERVATION_HOURS * 3600  # 28800
# Sampling ladder fitted to the 8h window.
OBSERVATION_OFFSETS = (
    0,
    15 * 60,
    60 * 60,
    2 * 60 * 60,
    4 * 60 * 60,
    6 * 60 * 60,
    8 * 60 * 60,
)
MAX_WINDOW_RESTARTS = 1
CLOCK_SKEW_SECONDS = 5


def native_observation_clock() -> dict[str, Any]:
    """Acquire actual owner time; request/sample fields never supply this clock."""
    boot = str(UUID(Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()))
    return {'schema': 'code_observation_clock.v1', 'wall_utc': datetime.now(timezone.utc).isoformat(),
            'boot_id': boot, 'monotonic_ns': time.monotonic_ns()}


def verified_observation_clock(clock: Any) -> dict[str, Any]:
    if (not isinstance(clock, dict) or set(clock) != {'schema', 'wall_utc', 'boot_id', 'monotonic_ns'}
            or clock['schema'] != 'code_observation_clock.v1'
            or type(clock['monotonic_ns']) is not int or clock['monotonic_ns'] < 0
            or not isinstance(clock['wall_utc'], str)):
        raise ValueError('observation_clock_invalid')
    try:
        wall = datetime.fromisoformat(clock['wall_utc'].replace('Z', '+00:00'))
        if wall.tzinfo is None or wall.utcoffset() != timezone.utc.utcoffset(wall):
            raise ValueError('observation_clock_not_utc')
        if str(UUID(clock['boot_id'])) != clock['boot_id']:
            raise ValueError('observation_boot_identity_invalid')
    except (TypeError, AttributeError) as exc:
        raise ValueError('observation_clock_invalid') from exc
    return dict(clock)


def observation_clock_discontinuity(previous: Any, current: dict[str, Any], claimed_at: str) -> str:
    current = verified_observation_clock(current)
    wall = datetime.fromisoformat(current['wall_utc'])
    try:
        claimed = datetime.fromisoformat(claimed_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return 'caller_observation_time_untrusted'
    if (claimed.tzinfo is None or claimed.utcoffset() != timezone.utc.utcoffset(claimed)
            or abs((claimed - wall).total_seconds()) > CLOCK_SKEW_SECONDS):
        return 'caller_observation_time_untrusted'
    if previous is None:
        return ''
    try:
        previous = verified_observation_clock(previous)
    except ValueError:
        return 'previous_observation_clock_unverified'
    if previous['boot_id'] != current['boot_id']:
        return 'observation_boot_changed'
    mono = (current['monotonic_ns'] - previous['monotonic_ns']) / 1_000_000_000
    elapsed = (wall - datetime.fromisoformat(previous['wall_utc'])).total_seconds()
    if mono < 0 or elapsed < 0 or abs(elapsed - mono) > CLOCK_SKEW_SECONDS:
        return 'observation_clock_discontinuity'
    return ''


def observation_monotonic_complete(anchor: Any, current: Any) -> bool:
    try:
        anchor = verified_observation_clock(anchor)
        current = verified_observation_clock(current)
        return (anchor['boot_id'] == current['boot_id']
                and current['monotonic_ns'] - anchor['monotonic_ns'] >= DEFAULT_OBSERVATION_SECONDS * 1_000_000_000)
    except ValueError:
        return False


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
    the observation window closes. One witness per phase and the latest eight
    ticks stay bounded at 16 while preserving both coverage and
    consecutive-degradation checks.
    """
    witnesses: dict[int, int] = {}
    for index, sample in enumerate(samples):
        phase = observation_phase(start, parse_observation_time(str(sample.get("observed_at") or "")))
        if phase >= 0:
            witnesses.setdefault(phase, index)
    retained = set(witnesses.values()) | set(range(max(0, len(samples) - 8), len(samples)))
    return [sample for index, sample in enumerate(samples) if index in retained]


__all__ = [
    "OBSERVATION_HOURS",
    "DEFAULT_OBSERVATION_SECONDS",
    "OBSERVATION_OFFSETS",
    "observation_phase",
    "parse_observation_time",
    "compact_observation_samples",
]
