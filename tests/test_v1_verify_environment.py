"""CE-1: v1 verify subprocess must not inherit parent secrets."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from eimemory.governance.promotion_manager import (
    _run_patch_subprocess,
    _v1_verification_environment,
)


def test_v1_verification_environment_is_allowlist_only() -> None:
    env = _v1_verification_environment(cache_root="/tmp/cache-x")
    assert "PATH" in env
    assert env["PYTHONPYCACHEPREFIX"] == "/tmp/cache-x"
    assert "PYTEST_ADDOPTS" in env
    # No accidental parent bleed in the constructed mapping itself.
    assert "EIMEMORY_RECEIPT_HMAC_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert all(not key.lower().endswith(("token", "secret", "password", "credential")) for key in env)


def test_verify_subprocess_does_not_see_parent_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_RECEIPT_HMAC_KEY", "super-secret-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-secret")
    marker = tmp_path / "seen.txt"
    script = tmp_path / "probe.py"
    script.write_text(
        "import os\n"
        f"open(r'{marker}', 'w').write("
        "'|'.join(k for k in ('EIMEMORY_RECEIPT_HMAC_KEY','AWS_SECRET_ACCESS_KEY') if k in os.environ))\n"
    )
    completed = _run_patch_subprocess(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout_seconds=30,
        phase="verify",
    )
    assert completed.returncode == 0, completed.stderr
    assert marker.read_text() == ""
