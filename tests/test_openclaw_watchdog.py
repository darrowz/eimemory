from __future__ import annotations

from eimemory.ops.openclaw_watchdog import parse_stuck_session_ages, should_restart_gateway


def test_parse_stuck_session_ages_from_gateway_logs() -> None:
    logs = """
2026-04-28T09:52:57 [diagnostic] stuck session: sessionId=main state=processing age=150s queueDepth=1
2026-04-28T09:53:57 [diagnostic] stuck session: sessionId=main state=processing age=210s queueDepth=1
"""

    assert parse_stuck_session_ages(logs) == [150, 210]


def test_watchdog_restarts_only_after_threshold_and_cooldown() -> None:
    assert should_restart_gateway(
        stuck_ages=[90, 150],
        threshold_s=120,
        last_restart_ts=1000.0,
        now_ts=1401.0,
        min_restart_interval_s=300,
    )
    assert not should_restart_gateway(
        stuck_ages=[150],
        threshold_s=120,
        last_restart_ts=1300.0,
        now_ts=1401.0,
        min_restart_interval_s=300,
    )


def test_watchdog_restart_requires_all_configured_health_checks_to_fail() -> None:
    base = {
        "stuck_ages": [150],
        "threshold_s": 120,
        "last_restart_ts": 1000.0,
        "now_ts": 1401.0,
        "min_restart_interval_s": 300,
    }

    assert not should_restart_gateway(**base, health_checks=[False, True])
    assert should_restart_gateway(**base, health_checks=[False, False])
    assert should_restart_gateway(**base, health_checks=[])
