"""Audit helpers must load trusted hosts before rejecting unknown SSH keys."""
import ast
from pathlib import Path
from types import SimpleNamespace
import os

import pytest


HELPERS = (
    "final-verify", "fix1a-unlock", "fix1b-wait", "fix1c-wait", "fix1d-analyze",
    "fix2-timers", "fix2b-verify", "fix3a-oom", "fix3b-health", "probe-timerdefs",
    "probe1b", "probe2-timers", "probe3-deep", "probe4-verify",
)


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize("opt_in", [False, True])
def test_ssh_setup_loads_known_hosts_before_policy(name, opt_in, monkeypatch):
    if opt_in:
        monkeypatch.setenv("EIMEMORY_SSH_TRUST_NEW_HOST", "1")
    else:
        monkeypatch.delenv("EIMEMORY_SSH_TRUST_NEW_HOST", raising=False)
    calls = []
    client = SimpleNamespace(
        load_system_host_keys=lambda: calls.append("known_hosts"),
        set_missing_host_key_policy=lambda policy: calls.append(policy),
    )
    source = Path("docs/audit", name + ".py").read_text(encoding="utf-8")
    setup = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            function = node.value.func
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                if function.value.id == "client":
                    if function.attr == "connect":
                        break
                    setup.append(node)
    # Execute only client setup, never imports, remote connections or commands.
    exec(compile(ast.Module(body=setup, type_ignores=[]), name, "exec"), {
        "client": client, "os": os,
        "paramiko": SimpleNamespace(RejectPolicy=lambda: "reject", AutoAddPolicy=lambda: "opt_in"),
    })
    assert calls == ["known_hosts", "opt_in" if opt_in else "reject"]
