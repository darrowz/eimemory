"""Hard kill switch for any autonomous eimemory process. Always available.

Cross-platform implementation:
- Windows: ``signal.SIGTERM`` is mapped by the standard signal module to
  ``TerminateProcess`` (no ``SIGKILL`` concept on Windows). Process groups
  are not first-class on Windows, so ``scope_to_pgid`` is treated as a
  hint to also kill child PIDs (best-effort: we walk the process tree
  via ``taskkill /T``). The audit log is written under ``%LOCALAPPDATA%``
  since ``/var/lib/eimemory`` is not writable.
- POSIX: the original ``pkill -9 -f eimemory`` / ``kill -<pgid> SIGKILL``
  semantics are preserved.

The function is idempotent: repeated calls are safe, and an unknown pid
is a no-op (ProcessLookupError is swallowed).

Audit failures never block the kill.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _audit_path() -> Path:
    """Resolve where the audit row should be written.

    Respects ``EIMEMORY_AUDIT_PATH`` if set. On Windows we fall back to
    ``%LOCALAPPDATA%\\eimemory\\state\\audit.jsonl`` because
    ``/var/lib/eimemory`` is not writable there.
    """
    env = os.environ.get("EIMEMORY_AUDIT_PATH")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "eimemory" / "state" / "audit.jsonl"
    return Path("/var/lib/eimemory/state/audit.jsonl")


def emergency_stop(*, pid: int | None = None, scope_to_pgid: bool = True) -> None:
    """Terminate all eimemory processes (or one PID group). Idempotent."""
    if pid is None:
        # Kill anything matching eimemory
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "eimemory.exe"],
                check=False,
                capture_output=True,
            )
        else:
            subprocess.run(["pkill", "-9", "-f", "eimemory"], check=False)
    else:
        if sys.platform == "win32":
            # /T = tree (kills the process and any children — closest
            # equivalent of "process group" on Windows).
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
            )
        else:
            target = -pid if scope_to_pgid else pid
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _append_audit({"event": "emergency_stop", "at": _now_iso(), "pid": pid})


def _append_audit(row: dict) -> None:
    """Append an audit row. Never raises — audit failure must not block the kill."""
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    emergency_stop()
