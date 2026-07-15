from __future__ import annotations

import subprocess
from pathlib import Path

from eimemory.ops import openclaw_watchdog as watchdog_module
from eimemory.ops.openclaw_watchdog import (
    collect_hook_pressure,
    parse_stuck_session_ages,
    resolve_unit_control_group,
    should_restart_gateway,
)


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
