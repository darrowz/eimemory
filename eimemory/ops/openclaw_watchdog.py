from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


STUCK_SESSION_PATTERN = re.compile(
    r"(?:stuck|stalled) session: .*?\bsessionId=([^\s]+).*?\bage=(\d+)s\b"
)
STUCK_SESSION_AGE_PATTERN = re.compile(r"(?:stuck|stalled) session: .*?\bage=(\d+)s\b")
PROC_RSS_PATTERN = re.compile(r"^VmRSS:\s+(\d+)\s+kB$", re.MULTILINE)


@dataclass(frozen=True)
class StuckSession:
    session_id: str
    age_s: int


@dataclass(frozen=True)
class CgroupPressure:
    memory_current_bytes: int = 0
    memory_high_bytes: int = 0
    memory_max_bytes: int = 0
    pids_current: int = 0
    pids_max: int = 0


def parse_stuck_sessions(log_text: str) -> list[StuckSession]:
    return [
        StuckSession(session_id=match.group(1), age_s=int(match.group(2)))
        for match in STUCK_SESSION_PATTERN.finditer(log_text)
    ]


def parse_stuck_session_ages(log_text: str) -> list[int]:
    return [int(match.group(1)) for match in STUCK_SESSION_AGE_PATTERN.finditer(log_text)]


def restart_trigger(
    *,
    stuck_ages: list[int],
    threshold_s: int,
    health_checks: list[bool] | None,
    hook_count: int,
    hook_rss_kib: int,
    max_hook_processes: int,
    max_hook_rss_kib: int,
    hook_pressure_streak: int,
    min_hook_pressure_samples: int,
    cgroup_pressure: CgroupPressure | None,
    max_memory_high_ratio: float,
    max_pids_ratio: float,
    cgroup_pressure_streak: int = 1,
    min_cgroup_pressure_samples: int = 1,
) -> str | None:
    hook_pressure = has_hook_pressure(
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
    )
    if hook_pressure and hook_pressure_streak >= max(1, min_hook_pressure_samples):
        return "hook_pressure"
    if cgroup_pressure:
        cgroup_pressure_active = has_cgroup_pressure(
            cgroup_pressure,
            max_memory_high_ratio=max_memory_high_ratio,
            max_pids_ratio=max_pids_ratio,
        )
        if (
            cgroup_pressure_active
            and cgroup_pressure_streak >= max(1, min_cgroup_pressure_samples)
        ):
            return "cgroup_pressure"
    if health_checks and any(health_checks):
        return None
    if stuck_ages and max(stuck_ages) >= threshold_s:
        return "stuck_session"
    return None


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
    cgroup_pressure: CgroupPressure | None = None,
    max_memory_high_ratio: float = 1.0,
    max_pids_ratio: float = 1.0,
    cgroup_pressure_streak: int = 1,
    min_cgroup_pressure_samples: int = 1,
) -> bool:
    if now_ts - last_restart_ts < min_restart_interval_s:
        return False
    return restart_trigger(
        stuck_ages=stuck_ages,
        threshold_s=threshold_s,
        health_checks=health_checks,
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
        hook_pressure_streak=hook_pressure_streak,
        min_hook_pressure_samples=min_hook_pressure_samples,
        cgroup_pressure=cgroup_pressure,
        max_memory_high_ratio=max_memory_high_ratio,
        max_pids_ratio=max_pids_ratio,
        cgroup_pressure_streak=cgroup_pressure_streak,
        min_cgroup_pressure_samples=min_cgroup_pressure_samples,
    ) is not None


def has_hook_pressure(
    *,
    hook_count: int,
    hook_rss_kib: int,
    max_hook_processes: int,
    max_hook_rss_kib: int,
) -> bool:
    return (
        (max_hook_processes > 0 and hook_count >= max_hook_processes)
        or (max_hook_rss_kib > 0 and hook_rss_kib >= max_hook_rss_kib)
    )


def _read_cgroup_scalar(path: Path) -> int:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if value == "max":
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def collect_cgroup_pressure(
    control_group: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> CgroupPressure:
    if not control_group:
        return CgroupPressure()
    cgroup_path = cgroup_root / control_group.lstrip("/")
    return CgroupPressure(
        memory_current_bytes=_read_cgroup_scalar(cgroup_path / "memory.current"),
        memory_high_bytes=_read_cgroup_scalar(cgroup_path / "memory.high"),
        memory_max_bytes=_read_cgroup_scalar(cgroup_path / "memory.max"),
        pids_current=_read_cgroup_scalar(cgroup_path / "pids.current"),
        pids_max=_read_cgroup_scalar(cgroup_path / "pids.max"),
    )


def has_cgroup_pressure(
    pressure: CgroupPressure,
    *,
    max_memory_high_ratio: float,
    max_pids_ratio: float,
) -> bool:
    memory_limit = pressure.memory_high_bytes or pressure.memory_max_bytes
    return (
        (
            memory_limit > 0
            and pressure.memory_current_bytes >= memory_limit * max_memory_high_ratio
        )
        or (
            pressure.pids_max > 0
            and pressure.pids_current >= pressure.pids_max * max_pids_ratio
        )
    )


def write_recovery_quarantine(
    path: Path,
    *,
    trigger: str,
    now_ts: float,
    ttl_s: int,
    sessions: list[StuckSession],
) -> dict:
    session_ids = [session.session_id for session in sessions]
    quarantine = {
        "schema": "openclaw_recovery_quarantine.v1",
        "trigger": trigger,
        "created_at_ts": now_ts,
        "expires_at_ts": now_ts + ttl_s,
        "mode": "targeted" if session_ids else "all_previous_lifecycle",
        "session_ids": session_ids,
        "consumed": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(quarantine, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_parent_directory(path.parent)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return quarantine


def invalidate_recovery_quarantine(path: Path) -> Path | None:
    invalid_path = path.with_name(f"{path.name}.restart-failed")
    try:
        os.replace(path, invalid_path)
    except FileNotFoundError:
        return None
    _fsync_parent_directory(path.parent)
    return invalid_path


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    cgroup_pressure_streak: int,
    cgroup_pressure_sample_ts: float,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_restart_ts": last_restart_ts,
                "max_stuck_age_s": max_stuck_age_s,
                "hook_pressure_streak": hook_pressure_streak,
                "hook_pressure_sample_ts": hook_pressure_sample_ts,
                "cgroup_pressure_streak": cgroup_pressure_streak,
                "cgroup_pressure_sample_ts": cgroup_pressure_sample_ts,
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
        cgroup_pressure_streak=0,
        cgroup_pressure_sample_ts=0.0,
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
    parser.add_argument("--max-memory-high-ratio", type=float, default=0.85)
    parser.add_argument("--max-pids-ratio", type=float, default=0.70)
    parser.add_argument("--min-cgroup-pressure-samples", type=int, default=2)
    parser.add_argument("--cgroup-pressure-sample-window-s", type=float, default=180.0)
    parser.add_argument(
        "--quarantine-path",
        default="/var/lib/eimemory/openclaw_recovery_quarantine.json",
    )
    parser.add_argument("--quarantine-ttl-s", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    journal_text = read_unit_journal(args.unit, args.since)
    stuck_sessions = parse_stuck_sessions(journal_text)
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
    cgroup_pressure = collect_cgroup_pressure(control_group)
    cgroup_pressure_active = has_cgroup_pressure(
        cgroup_pressure,
        max_memory_high_ratio=args.max_memory_high_ratio,
        max_pids_ratio=args.max_pids_ratio,
    )
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
    try:
        previous_cgroup_pressure_streak = int(
            watchdog_state.get("cgroup_pressure_streak") or 0
        )
    except (TypeError, ValueError):
        previous_cgroup_pressure_streak = 0
    try:
        previous_cgroup_pressure_sample_ts = float(
            watchdog_state.get("cgroup_pressure_sample_ts") or 0.0
        )
    except (TypeError, ValueError):
        previous_cgroup_pressure_sample_ts = 0.0
    cgroup_pressure_streak = next_hook_pressure_streak(
        pressure=cgroup_pressure_active,
        previous_streak=previous_cgroup_pressure_streak,
        previous_sample_ts=previous_cgroup_pressure_sample_ts,
        now_ts=now_ts,
        sample_window_s=float(args.cgroup_pressure_sample_window_s),
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
        cgroup_pressure=cgroup_pressure,
        max_memory_high_ratio=args.max_memory_high_ratio,
        max_pids_ratio=args.max_pids_ratio,
        cgroup_pressure_streak=cgroup_pressure_streak,
        min_cgroup_pressure_samples=args.min_cgroup_pressure_samples,
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
                cgroup_pressure_streak=cgroup_pressure_streak,
                cgroup_pressure_sample_ts=now_ts if cgroup_pressure_active else 0.0,
            )
        action = (
            "defer"
            if (
                hook_pressure
                and hook_pressure_streak < max(1, args.min_hook_pressure_samples)
            )
            or (
                cgroup_pressure_active
                and cgroup_pressure_streak
                < max(1, args.min_cgroup_pressure_samples)
            )
            else "none"
        )
        memory_limit_bytes = (
            cgroup_pressure.memory_high_bytes or cgroup_pressure.memory_max_bytes
        )
        print(
            f"openclaw_watchdog action={action} "
            f"stuck_ages={stuck_ages} health_checks={health_checks} "
            f"hook_count={hook_count} hook_rss_kib={hook_rss_kib} "
            f"hook_pressure_streak={hook_pressure_streak} "
            f"cgroup_pressure_streak={cgroup_pressure_streak} "
            f"memory_current_bytes={cgroup_pressure.memory_current_bytes} "
            f"memory_limit_bytes={memory_limit_bytes} "
            f"pids_current={cgroup_pressure.pids_current} "
            f"pids_max={cgroup_pressure.pids_max}"
        )
        return 0

    max_age = max(stuck_ages, default=0)
    trigger = restart_trigger(
        stuck_ages=stuck_ages,
        threshold_s=args.threshold_s,
        health_checks=health_checks,
        hook_count=hook_count,
        hook_rss_kib=hook_rss_kib,
        max_hook_processes=args.max_hook_processes,
        max_hook_rss_kib=max_hook_rss_kib,
        hook_pressure_streak=hook_pressure_streak,
        min_hook_pressure_samples=args.min_hook_pressure_samples,
        cgroup_pressure=cgroup_pressure,
        max_memory_high_ratio=args.max_memory_high_ratio,
        max_pids_ratio=args.max_pids_ratio,
        cgroup_pressure_streak=cgroup_pressure_streak,
        min_cgroup_pressure_samples=args.min_cgroup_pressure_samples,
    )
    assert trigger is not None
    print(
        f"openclaw_watchdog action=restart unit={args.unit} trigger={trigger} "
        f"max_stuck_age_s={max_age} hook_count={hook_count} hook_rss_kib={hook_rss_kib}"
        f" hook_pressure_streak={hook_pressure_streak}"
        f" cgroup_pressure_streak={cgroup_pressure_streak}"
        f" memory_current_bytes={cgroup_pressure.memory_current_bytes}"
        f" memory_limit_bytes="
        f"{cgroup_pressure.memory_high_bytes or cgroup_pressure.memory_max_bytes}"
        f" pids_current={cgroup_pressure.pids_current}"
        f" pids_max={cgroup_pressure.pids_max}"
    )
    if not args.dry_run:
        try:
            write_recovery_quarantine(
                Path(args.quarantine_path),
                trigger=trigger,
                now_ts=now_ts,
                ttl_s=args.quarantine_ttl_s,
                sessions=stuck_sessions,
            )
        except OSError as error:
            print(f"openclaw_watchdog action=quarantine_failed error={error}")
            return 2
        try:
            subprocess.run(["systemctl", "--user", "restart", args.unit], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            try:
                invalidate_recovery_quarantine(Path(args.quarantine_path))
            except OSError as cleanup_error:
                print(
                    "openclaw_watchdog action=quarantine_cleanup_failed "
                    f"restart_error={error} cleanup_error={cleanup_error}"
                )
                return 2
            print(f"openclaw_watchdog action=restart_failed error={error}")
            return 2
        save_restart_state(state_path, restarted_at_ts=now_ts, max_stuck_age_s=max_age)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
