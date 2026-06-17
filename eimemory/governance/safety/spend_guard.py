"""Hostname check that blocks any paid-API call from non-sandbox hosts.

This gate enforces the 2026-06-17 spec's non-negotiable rule: the
``honxin`` production host cannot autonomously spend on paid APIs.
Every code path that would call a paid API must invoke
``assert_no_paid_spend(call_site=...)`` first; on a non-sandbox host
the call raises ``PaidApiBlocked`` and the paid API is never reached.

The allow-list is read from a sandbox-hostnames file. The path is
resolved as follows (in order):
  1. ``EIMEMORY_SANDBOX_HOSTNAMES_FILE`` environment variable (used
     by tests and by ops who want to override the default).
  2. On Windows: ``%LOCALAPPDATA%\\eimemory\\state\\sandbox_hostnames.txt``
     (because ``/var/lib/eimemory`` is not writable on Windows).
  3. POSIX default: ``/var/lib/eimemory/state/sandbox_hostnames.txt``.

The contract is fail-closed: if the file is missing, the allow-list
is empty, and every host is blocked. The operator must explicitly
declare a sandbox for the gate to allow a call.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _default_sandbox_hostnames_file() -> Path:
    """Resolve the default sandbox-hostnames file path for this platform.

    Honors ``EIMEMORY_SANDBOX_HOSTNAMES_FILE`` if set, then falls back
    to a platform-appropriate default. On Windows we use
    ``%LOCALAPPDATA%\\eimemory\\state\\sandbox_hostnames.txt`` because
    the POSIX default path is not writable.
    """
    env = os.environ.get("EIMEMORY_SANDBOX_HOSTNAMES_FILE")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "eimemory" / "state" / "sandbox_hostnames.txt"
    return Path("/var/lib/eimemory/state/sandbox_hostnames.txt")


def _sandbox_hostnames(path: Path | None = None) -> set[str]:
    """Read the sandbox-hostnames file. Empty set when the file is missing.

    The returned set is the explicit allow-list of hosts that are
    authorized to make paid API calls. Empty set = fail-closed (no
    host is allowed).
    """
    file_path = path if path is not None else _default_sandbox_hostnames_file()
    if not file_path.exists():
        return set()
    return {
        line.strip()
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


class PaidApiBlocked(Exception):
    """Raised when a paid-API call would originate from a non-sandbox host."""

    def __init__(self, hostname: str, call_site: str) -> None:
        self.hostname = hostname
        self.call_site = call_site
        super().__init__(f"paid_api_blocked on {hostname} at {call_site}")


def assert_no_paid_spend(*, call_site: str) -> None:
    """Refuse paid API spend unless the current host is on the sandbox list.

    ``call_site`` is a free-form identifier (e.g. ``"loop.py:hypothesis_gen"``)
    that is recorded on the ``PaidApiBlocked`` exception so triage can
    locate the exact code path that tried to spend.
    """
    hostname = socket.gethostname()
    if hostname not in _sandbox_hostnames():
        log.warning(
            "paid_api_blocked hostname=%s call_site=%s",
            hostname, call_site,
        )
        raise PaidApiBlocked(hostname, call_site)
