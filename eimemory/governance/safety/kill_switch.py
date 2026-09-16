"""Hard kill switch for any autonomous eimemory process. Always available.

Cross-platform implementation:
- Windows: ``signal.SIGTERM`` is mapped by the standard signal module to
  ``TerminateProcess`` (no ``SIGKILL`` concept on Windows). Process groups
  are not first-class on Windows, so ``scope_to_pgid`` is treated as a
  hint to also kill child PIDs (best-effort: we walk the process tree
  via ``taskkill /T``). The audit log is written under ``%LOCALAPPDATA%``
  since ``/var/lib/eimemory`` is not writable.
- POSIX: kill by explicit PID or process group only. Broad ``pkill -f``
  matching is intentionally removed — it could SIGKILL unrelated processes
  whose command lines contain the substring ``eimemory``.

Audit is written BEFORE the kill so a successful self-termination still
leaves a durable trail when the audit path is writable. Audit failures
never block the kill.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from eimemory.governance.safety.audit import AuditLog


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
    """Terminate one PID (or its process group). Idempotent.

    When ``pid`` is omitted, only the current process is targeted (or its
    process group when ``scope_to_pgid`` is True). Broad substring process
    matching is never used.
    """
    target_pid = int(pid) if pid is not None else os.getpid()
    _append_audit(
        {
            "event": "emergency_stop",
            "at": _now_iso(),
            "pid": target_pid,
            "requested_pid": pid,
            "scope_to_pgid": bool(scope_to_pgid),
        }
    )
    if sys.platform == "win32":
        # /T = tree (kills the process and any children — closest
        # equivalent of "process group" on Windows).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(target_pid)],
            check=False,
            capture_output=True,
        )
        return
    try:
        if scope_to_pgid:
            os.killpg(os.getpgid(target_pid), signal.SIGKILL)
        else:
            os.kill(target_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Best-effort: fall back to single-PID kill if pgid is inaccessible.
        try:
            os.kill(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _append_audit(row: dict) -> None:
    """Append an audit row. Never raises — audit failure must not block the kill."""
    try:
        # Append via AuditLog so the sha256 chain is maintained.  Writing
        # plain rows here would poison the chain and re-trigger the very
        # ChainBroken -> emergency_stop loop this guard exists to serve.
        AuditLog(_audit_path()).append(dict(row))
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    emergency_stop(pid=os.getpid(), scope_to_pgid=False)
