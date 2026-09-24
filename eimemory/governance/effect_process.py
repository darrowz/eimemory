"""Bounded POSIX process-group lifetime for transaction-owned commands.

A process group is cancellation ownership, not a security sandbox. Verification
must still use its OS sandbox. A deliberately detached service requires separate
service/cgroup ownership and deployment reconciliation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


@dataclass(frozen=True)
class OwnedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def _stop_group(process: subprocess.Popen, *, grace: float = 0.15) -> None:
    # Do not depend on the group leader still being alive: a grandchild may
    # outlive it or keep an inherited output descriptor open.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        time.sleep(grace)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        # Do not publish completion while the owner cannot reap its leader.
        raise RuntimeError("protected_process_cleanup_unconfirmed") from exc


def run_owned_process(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    heartbeat: Callable[[], object] | None = None,
    max_seconds: float = 7200.0,
    heartbeat_seconds: float = 30.0,
    output_limit: int = 1024 * 1024,
    merge_stderr: bool = True,
) -> OwnedProcessResult:
    """Drain bounded output; cancel the entire owned group on every exit path."""
    if os.name != "posix":
        raise RuntimeError("protected_process_group_unavailable")
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv_required")
    if not all(isinstance(item, (str, Path)) and str(item) for item in argv):
        raise ValueError("argv_invalid")
    timeout, interval = float(max_seconds), float(heartbeat_seconds)
    if not isfinite(timeout) or timeout <= 0 or not isfinite(interval) or interval <= 0:
        raise ValueError("process_time_budget_invalid")
    if type(output_limit) is not int or not 0 <= output_limit <= 16 * 1024 * 1024:
        raise ValueError("process_output_budget_invalid")
    # A lost lease must be discovered BEFORE a new subprocess can have effects.
    if heartbeat is not None:
        heartbeat()
    started = time.monotonic()
    process = subprocess.Popen(
        [str(item) for item in argv], cwd=cwd, env=dict(env), shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        start_new_session=True, bufsize=0,
    )
    selector = None
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    output = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = False
    group_stopped = False
    try:
        selector = selectors.DefaultSelector()
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ, name)
        if process.stdout is None:
            raise RuntimeError("protected_process_output_pipe_unavailable")
        deadline = started + timeout
        next_heartbeat = started + interval
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            if heartbeat is not None and now >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + interval
            code = process.poll()
            if code is not None and not group_stopped:
                _stop_group(process)
                group_stopped = True
            if code is not None and not selector.get_map():
                break
            wait = max(0.0, min(0.05, deadline - now,
                                next_heartbeat - now if heartbeat is not None else 0.05))
            for key, _ in selector.select(wait):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = output_limit - sum(map(len, output.values()))
                if remaining > 0:
                    output[key.data].extend(chunk[:remaining])
        if heartbeat is not None and not timed_out:
            heartbeat()
    finally:
        try:
            if not group_stopped:
                _stop_group(process)
        finally:
            if selector is not None:
                selector.close()
            for stream in streams:
                stream.close()
    return OwnedProcessResult(
        124 if timed_out else int(process.returncode),
        bytes(output["stdout"]), bytes(output["stderr"]), timed_out,
    )
