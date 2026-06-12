from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
import urllib.request


STUCK_SESSION_PATTERN = re.compile(r"stuck session: .*?\bage=(\d+)s\b")


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
) -> bool:
    if health_checks and any(health_checks):
        return False
    return (
        bool(stuck_ages)
        and max(stuck_ages) >= threshold_s
        and now_ts - last_restart_ts >= min_restart_interval_s
    )


def load_last_restart_ts(state_path: Path) -> float:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    try:
        return float(data.get("last_restart_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def save_restart_state(state_path: Path, *, restarted_at_ts: float, max_stuck_age_s: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_restart_ts": restarted_at_ts,
                "max_stuck_age_s": max_stuck_age_s,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    journal = subprocess.run(
        ["journalctl", "--user", "-u", args.unit, "--since", args.since, "--no-pager"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stuck_ages = parse_stuck_session_ages(journal.stdout)
    now_ts = time.time()
    state_path = Path(args.state_path)
    last_restart_ts = load_last_restart_ts(state_path)
    health_urls = [str(url) for url in list(args.health_url or []) + list(args.loopback_health_url or []) if str(url)]
    health_checks = [probe_health_url(url, timeout_s=float(args.health_timeout_s)) for url in health_urls]
    if not should_restart_gateway(
        stuck_ages=stuck_ages,
        threshold_s=args.threshold_s,
        last_restart_ts=last_restart_ts,
        now_ts=now_ts,
        min_restart_interval_s=args.min_restart_interval_s,
        health_checks=health_checks,
    ):
        print(f"openclaw_watchdog action=none stuck_ages={stuck_ages} health_checks={health_checks}")
        return 0

    max_age = max(stuck_ages)
    print(f"openclaw_watchdog action=restart unit={args.unit} max_stuck_age_s={max_age}")
    if not args.dry_run:
        subprocess.run(["systemctl", "--user", "restart", args.unit], check=True)
        save_restart_state(state_path, restarted_at_ts=now_ts, max_stuck_age_s=max_age)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
