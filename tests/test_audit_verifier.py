"""Tests for the hourly audit chain verifier (Task 0.5).

The verifier's only job is to call :func:`emergency_stop` when it sees
a broken chain.  A clean log must be a no-op.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from eimemory.governance.safety.audit import AuditLog
from eimemory.governance.safety.audit_verifier import AuditVerifier


def test_verifier_runs_hourly_clean_log_does_nothing(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    al = AuditLog(log_path)
    for i in range(10):
        al.append({"n": i})
    with patch("eimemory.governance.safety.audit_verifier.emergency_stop") as mock_kill:
        v = AuditVerifier(log_path=log_path)
        v.verify_once()
    mock_kill.assert_not_called()


def test_verifier_runs_hourly_tampered_log_calls_kill_switch(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    al = AuditLog(log_path)
    for i in range(10):
        al.append({"n": i})
    # Tamper with row 3: rewrite the payload but keep the stale row_hash.
    rows = log_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[3])
    payload["n"] = 999
    rows[3] = json.dumps(payload)
    log_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with patch("eimemory.governance.safety.audit_verifier.emergency_stop") as mock_kill:
        v = AuditVerifier(log_path=log_path)
        v.verify_once()
    mock_kill.assert_called_once()
