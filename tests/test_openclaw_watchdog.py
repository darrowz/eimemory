from __future__ import annotations

import subprocess
from pathlib import Path

from eimemory.ops import openclaw_watchdog as watchdog_module
from eimemory.ops.openclaw_watchdog import (
    collect_hook_pressure,
    next_hook_pressure_streak,
    parse_stuck_session_ages,
    resolve_unit_control_group,
    should_restart_gateway,
)


def test_parse_stuck_session_ages_from_gateway_logs() -> None:
    logs = """
2026-04-28T09:52:57 [diagnostic] stuck session: sessionId=main state=processing age=150s queueDepth=1
2026-04-28T09:53:57 [diagnostic] stuck session: sessionId=main state=processing age=210s queueDepth=1
2026-07-16T01:10:54 [diagnostic] stalled session: sessionId=main state=processing age=125s queueDepth=1
"""

    assert parse_stuck_session_ages(logs) == [150, 210, 125]


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


def test_watchdog_restarts_on_hook_pressure_even_when_health_probe_passes() -> None:
    assert should_restart_gateway(
        stuck_ages=[],
        threshold_s=120,
        last_restart_ts=1000.0,
        now_ts=1401.0,
        min_restart_interval_s=300,
        health_checks=[True],
        hook_count=9,
        hook_rss_kib=512_000,
        max_hook_processes=8,
        max_hook_rss_kib=1_572_864,
    )
    assert should_restart_gateway(
        stuck_ages=[],
        threshold_s=120,
        last_restart_ts=1000.0,
        now_ts=1401.0,
        min_restart_interval_s=300,
        health_checks=[True],
        hook_count=2,
        hook_rss_kib=1_572_865,
        max_hook_processes=8,
        max_hook_rss_kib=1_572_864,
    )


def test_watchdog_requires_consecutive_hook_pressure_samples() -> None:
    base = {
        "stuck_ages": [],
        "threshold_s": 120,
        "last_restart_ts": 1000.0,
        "now_ts": 1401.0,
        "min_restart_interval_s": 300,
        "health_checks": [True],
        "hook_count": 8,
        "hook_rss_kib": 3_697_364,
        "max_hook_processes": 8,
        "max_hook_rss_kib": 3_145_728,
        "min_hook_pressure_samples": 2,
    }

    assert not should_restart_gateway(**base, hook_pressure_streak=1)
    assert should_restart_gateway(**base, hook_pressure_streak=2)


def test_hook_pressure_streak_only_counts_recent_consecutive_samples() -> None:
    assert next_hook_pressure_streak(
        pressure=True,
        previous_streak=0,
        previous_sample_ts=0.0,
        now_ts=100.0,
        sample_window_s=180,
    ) == 1
    assert next_hook_pressure_streak(
        pressure=True,
        previous_streak=1,
        previous_sample_ts=100.0,
        now_ts=220.0,
        sample_window_s=180,
    ) == 2
    assert next_hook_pressure_streak(
        pressure=True,
        previous_streak=2,
        previous_sample_ts=100.0,
        now_ts=400.0,
        sample_window_s=180,
    ) == 1
    assert next_hook_pressure_streak(
        pressure=False,
        previous_streak=2,
        previous_sample_ts=220.0,
        now_ts=280.0,
        sample_window_s=180,
    ) == 0


def test_collect_hook_pressure_reads_only_openclaw_hook_processes(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    control_group = "/user.slice/openclaw-gateway.service"
    cgroup_path = cgroup_root / control_group.lstrip("/")
    cgroup_path.mkdir(parents=True)
    (cgroup_path / "cgroup.procs").write_text("101\n102\n103\n", encoding="utf-8")

    for pid, command, rss_kib in (
        (101, "openclaw-hooks", 220_000),
        (102, "node", 500_000),
        (103, "openclaw-hooks", 330_000),
    ):
        process_path = proc_root / str(pid)
        process_path.mkdir(parents=True)
        (process_path / "comm").write_text(f"{command}\n", encoding="utf-8")
        (process_path / "status").write_text(
            f"Name:\t{command}\nVmRSS:\t{rss_kib} kB\n",
            encoding="utf-8",
        )

    assert collect_hook_pressure(
        control_group,
        cgroup_root=cgroup_root,
        proc_root=proc_root,
    ) == (2, 550_000)


def test_collect_hook_pressure_ignores_fresh_hook_processes(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    control_group = "/user.slice/openclaw-gateway.service"
    cgroup_path = cgroup_root / control_group.lstrip("/")
    cgroup_path.mkdir(parents=True)
    (cgroup_path / "cgroup.procs").write_text("101\n102\n", encoding="utf-8")

    for pid, start_ticks in ((101, 9_500), (102, 8_000)):
        process_path = proc_root / str(pid)
        process_path.mkdir(parents=True)
        (process_path / "comm").write_text("openclaw-hooks\n", encoding="utf-8")
        (process_path / "status").write_text("VmRSS:\t500000 kB\n", encoding="utf-8")
        stat_fields = [str(pid), "(openclaw-hooks)", "S", *(["0"] * 18), str(start_ticks)]
        (process_path / "stat").write_text(" ".join(stat_fields) + "\n", encoding="utf-8")

    assert collect_hook_pressure(
        control_group,
        cgroup_root=cgroup_root,
        proc_root=proc_root,
        min_age_s=10,
        uptime_s=100.0,
        clock_ticks=100,
    ) == (1, 500_000)


def test_watchdog_external_reads_fail_open_on_timeout() -> None:
    def timeout_run(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=3)

    assert hasattr(watchdog_module, "read_unit_journal")
    assert resolve_unit_control_group("openclaw-gateway.service", run=timeout_run) == ""
    assert watchdog_module.read_unit_journal(
        "openclaw-gateway.service",
        "5 minutes ago",
        run=timeout_run,
    ) == ""
