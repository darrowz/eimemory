"""Tests for the hostname-based paid-API spend guard (Task 4.6).

The 2026-06-17 spec makes one rule non-negotiable: the ``honxin``
production host cannot autonomously spend on paid APIs. The
``assert_no_paid_spend`` gate is the fail-closed hook that enforces
this — every code path that would call a paid API must invoke it
first, and on a non-sandbox host it must raise.

Test contract (per the plan's Step 1):
- (a) hostname == "honxin" (production) -> PaidApiBlocked is raised.
- (b) hostname == "honxin-sandbox-1" (sandbox) -> call passes.

The allow-list lives in a sandbox-hostnames file. To stay portable
across POSIX/Windows and to make the test self-contained, the
implementation respects an ``EIMEMORY_SANDBOX_HOSTNAMES_FILE`` env
var pointing at the file; the test writes the file under ``tmp_path``.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from eimemory.governance.safety import spend_guard
from eimemory.governance.safety.spend_guard import (
    PaidApiBlocked,
    assert_no_paid_spend,
)


def _write_sandbox_hosts(path: Path, hosts: list[str]) -> None:
    """Write a sandbox-hostnames file consumed by the guard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(hosts) + "\n", encoding="utf-8")


def test_spend_guard_blocks_honxin_production_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``honxin`` (production) MUST raise PaidApiBlocked.

    The sandbox file is empty — production host is not on the allow-list.
    """
    sandbox_file = tmp_path / "sandbox_hostnames.txt"
    _write_sandbox_hosts(sandbox_file, [])
    monkeypatch.setenv("EIMEMORY_SANDBOX_HOSTNAMES_FILE", str(sandbox_file))
    monkeypatch.setattr(socket, "gethostname", lambda: "honxin")

    with pytest.raises(PaidApiBlocked) as excinfo:
        assert_no_paid_spend(call_site="loop.py:hypothesis_gen")

    assert excinfo.value.hostname == "honxin"
    assert excinfo.value.call_site == "loop.py:hypothesis_gen"


def test_spend_guard_allows_sandbox_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host listed in the sandbox file MUST pass (no exception)."""
    sandbox_file = tmp_path / "sandbox_hostnames.txt"
    _write_sandbox_hosts(sandbox_file, ["honxin-sandbox-1", "honxin-sandbox-2"])
    monkeypatch.setenv("EIMEMORY_SANDBOX_HOSTNAMES_FILE", str(sandbox_file))
    monkeypatch.setattr(socket, "gethostname", lambda: "honxin-sandbox-1")

    # Must not raise.
    assert_no_paid_spend(call_site="loop.py:hypothesis_gen")


def test_spend_guard_blocks_unknown_host_even_when_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing sandbox file = fail-closed: every host is blocked.

    Spec contract: the absence of an explicit sandbox allow-list must
    NOT be interpreted as "open season". A missing file means the
    operator has not declared any sandbox, so we raise on every call.
    """
    missing_file = tmp_path / "does_not_exist.txt"
    monkeypatch.setenv("EIMEMORY_SANDBOX_HOSTNAMES_FILE", str(missing_file))
    monkeypatch.setattr(socket, "gethostname", lambda: "honxin-sandbox-1")

    with pytest.raises(PaidApiBlocked) as excinfo:
        assert_no_paid_spend(call_site="loop.py:hypothesis_gen")

    assert excinfo.value.hostname == "honxin-sandbox-1"


def test_paid_api_blocked_carries_hostname_and_call_site() -> None:
    """PaidApiBlocked exposes both ``hostname`` and ``call_site`` for triage."""
    err = PaidApiBlocked(hostname="honxin", call_site="loop.py:rank")
    msg = str(err)
    assert "honxin" in msg
    assert "loop.py:rank" in msg


def test_spend_guard_module_is_importable() -> None:
    """Smoke check: the module exposes the public surface expected by callers."""
    assert hasattr(spend_guard, "assert_no_paid_spend")
    assert hasattr(spend_guard, "PaidApiBlocked")
