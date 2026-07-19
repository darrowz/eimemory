from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path


STUCK_SESSION_PATTERN = re.compile(r"(?:stuck|stalled) session: .*?\bage=(\d+)s\b")
PROC_RSS_PATTERN = re.compile(r"^VmRSS:\s+(\d+)\s+kB$", re.MULTILINE)


def parse_stuck_session_ages(log_text: str) -> list[int]:
    return [int(match.group(1)) for match in STUCK_SESSION_PATTERN.finditer(log_text)]


def should_restart_gateway(
    *,
    stuck_ages: list[int],
    threshold_s: int,
    last_restart_ts: float,
    now_ts: float,
    min_restart_interval_s: int,
    health_checks: list[bool] | None = None,
    hook_count: int = 0,
    hook_rss_kib: int = 0,
    max_hook_processes: int = 0,
    max_hook_rss_kib: int = 0,
    hook_pressure_streak: int = 1,
    min_hook_pressure_samples: int = 1,
) -> bool:
    if now_ts - last_restart_ts < min_restart_interval_s:
        return False
    hook_pressure = has_hook_pressure(
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
    )
    if hook_pressure and hook_pressure_streak >= max(1, min_hook_pressure_samples):
        return True
    if health_checks and any(health_checks):
        return False
    return bool(stuck_ages) and max(stuck_ages) >= threshold_s


def has_hook_pressure(
    *,
    hook_count: int,
    hook_rss_kib: int,
    max_hook_processes: int,
    max_hook_rss_kib: int,
) -> bool:
    return (
        (max_hook_processes > 0 and hook_count > max_hook_processes)
        or (max_hook_rss_kib > 0 and hook_rss_kib > max_hook_rss_kib)
    )


def next_hook_pressure_streak(
    *,
    pressure: bool,
    previous_streak: int,
    previous_sample_ts: float,
    now_ts: float,
    sample_window_s: float,
) -> int:
    if not pressure:
        return 0
    sample_is_recent = (
        previous_sample_ts > 0
        and now_ts >= previous_sample_ts
        and now_ts - previous_sample_ts <= max(0.0, sample_window_s)
    )
    return max(0, previous_streak) + 1 if sample_is_recent else 1


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def resolve_unit_control_group(
    unit: str,
    *,
    run: CommandRunner = subprocess.run,
) -> str:
    try:
        result = run(
            ["systemctl", "--user", "show", unit, "-p", "ControlGroup", "--value"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def read_unit_journal(
    unit: str,
    since: str,
    *,
    run: CommandRunner = subprocess.run,
) -> str:
    try:
        result = run(
            ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def collect_hook_pressure(
    control_group: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    proc_root: Path = Path("/proc"),
    min_age_s: float = 0,
    uptime_s: float | None = None,
    clock_ticks: int | None = None,
) -> tuple[int, int]:
    if not control_group:
        return 0, 0
    try:
        pid_lines = (cgroup_root / control_group.lstrip("/") / "cgroup.procs").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return 0, 0

    if min_age_s > 0 and uptime_s is None:
        try:
            uptime_s = float((proc_root / "uptime").read_text(encoding="utf-8").split()[0])
        except (OSError, IndexError, ValueError):
            uptime_s = None
    if min_age_s > 0 and clock_ticks is None:
        sysconf = getattr(os, "sysconf", None)
        try:
            clock_ticks = int(sysconf("SC_CLK_TCK")) if callable(sysconf) else None
        except (OSError, TypeError, ValueError):
            clock_ticks = None

    hook_count = 0
    hook_rss_kib = 0
    for pid_text in pid_lines:
        if not pid_text.isdigit():
            continue
        process_path = proc_root / pid_text
        try:
            command = (process_path / "comm").read_text(encoding="utf-8").strip()
            if command != "openclaw-hooks":
                continue
            status_text = (process_path / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        if min_age_s > 0 and uptime_s is not None and clock_ticks:
            try:
                stat_text = (process_path / "stat").read_text(encoding="utf-8")
                start_ticks = int(stat_text[stat_text.rfind(")") + 2 :].split()[19])
                process_age_s = uptime_s - (start_ticks / clock_ticks)
            except (OSError, IndexError, ValueError):
                process_age_s = min_age_s
            if process_age_s < min_age_s:
                continue
        hook_count += 1
        match = PROC_RSS_PATTERN.search(status_text)
        if match:
            hook_rss_kib += int(match.group(1))
    return hook_count, hook_rss_kib


def load_watchdog_state(state_path: Path) -> dict:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_last_restart_ts(state_path: Path) -> float:
    data = load_watchdog_state(state_path)
    try:
        return float(data.get("last_restart_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def save_watchdog_state(
    state_path: Path,
    *,
    last_restart_ts: float,
    max_stuck_age_s: int,
    hook_pressure_streak: int,
    hook_pressure_sample_ts: float,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_restart_ts": last_restart_ts,
                "max_stuck_age_s": max_stuck_age_s,
                "hook_pressure_streak": hook_pressure_streak,
                "hook_pressure_sample_ts": hook_pressure_sample_ts,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def save_restart_state(state_path: Path, *, restarted_at_ts: float, max_stuck_age_s: int) -> None:
    save_watchdog_state(
        state_path,
        last_restart_ts=restarted_at_ts,
        max_stuck_age_s=max_stuck_age_s,
        hook_pressure_streak=0,
        hook_pressure_sample_ts=0.0,
    )


def probe_health_url(url: str, *, timeout_s: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return response.status < 500 and payload.get("ok") is not False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart OpenClaw gateway when Feishu sessions stay stuck.")
    parser.add_argument("--unit", default="openclaw-gateway.service")
    parser.add_argument("--since", default="5 minutes ago")
    parser.add_argument("--threshold-s", type=int, default=120)
    parser.add_argument("--min-restart-interval-s", type=int, default=300)
    parser.add_argument("--state-path", default="/tmp/eimemory-openclaw-watchdog/state.json")
    parser.add_argument("--health-url", action="append", default=[])
    parser.add_argument("--loopback-health-url", action="append", default=[])
    parser.add_argument("--health-timeout-s", type=float, default=2.0)
    parser.add_argument("--max-hook-processes", type=int, default=8)
    parser.add_argument("--max-hook-rss-mib", type=int, default=1536)
    parser.add_argument("--min-hook-age-s", type=float, default=10.0)
    parser.add_argument("--min-hook-pressure-samples", type=int, default=1)
    parser.add_argument("--hook-pressure-sample-window-s", type=float, default=180.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    journal_text = read_unit_journal(args.unit, args.since)
    stuck_ages = parse_stuck_session_ages(journal_text)
    now_ts = time.time()
    state_path = Path(args.state_path)
    watchdog_state = load_watchdog_state(state_path)
    try:
        last_restart_ts = float(watchdog_state.get("last_restart_ts") or 0.0)
    except (TypeError, ValueError):
        last_restart_ts = 0.0
    health_urls = [
        str(url)
        for url in list(args.health_url or []) + list(args.loopback_health_url or [])
        if str(url)
    ]
    health_checks = [probe_health_url(url, timeout_s=float(args.health_timeout_s)) for url in health_urls]
    control_group = resolve_unit_control_group(args.unit)
    hook_count, hook_rss_kib = collect_hook_pressure(
        control_group,
        min_age_s=max(0.0, float(args.min_hook_age_s)),
    )
    max_hook_rss_kib = int(args.max_hook_rss_mib) * 1024
    hook_pressure = has_hook_pressure(
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=args.max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
    )
    try:
        previous_hook_pressure_streak = int(
            watchdog_state.get("hook_pressure_streak") or 0
        )
    except (TypeError, ValueError):
        previous_hook_pressure_streak = 0
    try:
        previous_hook_pressure_sample_ts = float(
            watchdog_state.get("hook_pressure_sample_ts") or 0.0
        )
    except (TypeError, ValueError):
        previous_hook_pressure_sample_ts = 0.0
    hook_pressure_streak = next_hook_pressure_streak(
        pressure=hook_pressure,
        previous_streak=previous_hook_pressure_streak,
        previous_sample_ts=previous_hook_pressure_sample_ts,
        now_ts=now_ts,
        sample_window_s=float(args.hook_pressure_sample_window_s),
    )
    if not should_restart_gateway(
        stuck_ages=stuck_ages,
        threshold_s=args.threshold_s,
        last_restart_ts=last_restart_ts,
        now_ts=now_ts,
        min_restart_interval_s=args.min_restart_interval_s,
        health_checks=health_checks,
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=args.max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
        hook_pressure_streak=hook_pressure_streak,
        min_hook_pressure_samples=args.min_hook_pressure_samples,
    ):
        if not args.dry_run:
            try:
                previous_max_stuck_age_s = int(
                    watchdog_state.get("max_stuck_age_s") or 0
                )
            except (TypeError, ValueError):
                previous_max_stuck_age_s = 0
            save_watchdog_state(
                state_path,
                last_restart_ts=last_restart_ts,
                max_stuck_age_s=previous_max_stuck_age_s,
                hook_pressure_streak=hook_pressure_streak,
                hook_pressure_sample_ts=now_ts if hook_pressure else 0.0,
            )
        action = (
            "defer"
            if hook_pressure
            and hook_pressure_streak < max(1, args.min_hook_pressure_samples)
            else "none"
        )
        print(
            f"openclaw_watchdog action={action} "
            f"stuck_ages={stuck_ages} health_checks={health_checks} "
            f"hook_count={hook_count} hook_rss_kib={hook_rss_kib} "
            f"hook_pressure_streak={hook_pressure_streak}"
        )
        return 0

    max_age = max(stuck_ages, default=0)
    trigger = "hook_pressure" if hook_pressure else "stuck_session"
    print(
        f"openclaw_watchdog action=restart unit={args.unit} trigger={trigger} "
        f"max_stuck_age_s={max_age} hook_count={hook_count} hook_rss_kib={hook_rss_kib}"
        f" hook_pressure_streak={hook_pressure_streak}"
    )
    if not args.dry_run:
        subprocess.run(["systemctl", "--user", "restart", args.unit], check=True)
        save_restart_state(state_path, restarted_at_ts=now_ts, max_stuck_age_s=max_age)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
