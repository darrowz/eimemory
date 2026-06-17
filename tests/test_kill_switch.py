"""Tests for the eimemory emergency-stop kill switch.

Covers Task 0.1 of the Karpathy Loop plan: a hard kill switch that
terminates long-running eimemory processes inside 5 seconds.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


def test_kill_switch_terminates_long_process(tmp_path: Path):
    """Start a sleep loop, invoke kill switch, verify it dies in <5s."""
    # Spawn a long-running eimemory-like process
    proc = subprocess.Popen(
        ["python", "-c", "import time; [time.sleep(1) for _ in range(600)]"],
    )
    time.sleep(0.5)  # let it start
    # Invoke kill switch on its PID group
    from eimemory.governance.safety.kill_switch import emergency_stop

    start = time.time()
    emergency_stop(pid=proc.pid, scope_to_pgid=True)
    proc.wait(timeout=5)
    elapsed = time.time() - start
    assert elapsed < 5
    assert proc.returncode != 0
