"""Wait for authenticated, read-only gateway RPC health after a restart."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def wait_ready(binary: str, config: str, *, timeout: float = 120, runner=subprocess.run,
               clock=time.monotonic, sleep=time.sleep) -> dict:
    if not Path(binary).is_absolute() or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        return {"ok": False, "reason": "binary_unavailable"}
    if not Path(config).is_absolute() or not Path(config).is_file():
        return {"ok": False, "reason": "config_unavailable"}
    try:
        configuration = json.loads(Path(config).read_text(encoding="utf-8"))
        if not isinstance(configuration, dict):
            raise ValueError("config must be an object")
        gateway = configuration.get("gateway", {})
        if not isinstance(gateway, dict):
            raise ValueError("gateway must be an object")
        # Official probe-target treats absent mode as local. A managed service
        # must never be declared ready by contacting a configured remote host.
        if gateway.get("mode", "local") != "local":
            return {"ok": False, "reason": "managed_gateway_requires_local_mode"}
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "reason": "config_invalid"}
    environment = {**os.environ, "OPENCLAW_CONFIG_PATH": config}
    # Do not let an inherited endpoint override redirect the readiness probe.
    environment.pop("OPENCLAW_GATEWAY_URL", None)
    environment.pop("OPENCLAW_GATEWAY_PORT", None)
    deadline = clock() + timeout
    consecutive = attempts = 0
    reason = "gateway_not_ready"
    while clock() < deadline:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        attempts += 1
        try:
            result = runner(
                [binary, "gateway", "health", "--json", "--timeout", str(max(1, int(min(10, remaining) * 1000)))],
                env=environment,
                shell=False, text=True, capture_output=True, timeout=min(15, remaining),
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else None
            healthy = (
                isinstance(payload, dict) and payload.get("ok") is True
                and isinstance(payload.get("channels"), dict)
                and isinstance(payload.get("agents"), list)
                and isinstance(payload.get("ts"), (int, float))
                and not isinstance(payload.get("ts"), bool)
            )
            reason = "gateway_health_unverified"
        except (OSError, subprocess.TimeoutExpired, ValueError):
            healthy = False
            reason = "gateway_health_unavailable"
        consecutive = consecutive + 1 if healthy else 0
        if consecutive >= 2:
            return {"ok": True, "attempts": attempts, "consecutive_passes": consecutive}
        sleep(max(0, min(2, deadline - clock())))
    return {"ok": False, "reason": reason, "attempts": attempts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    report = wait_ready(args.binary, args.config)
    # Never expose CLI output: authentication diagnostics can contain secrets.
    print(json.dumps({"openclaw_gateway_readiness": report}, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
