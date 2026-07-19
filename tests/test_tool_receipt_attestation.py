from __future__ import annotations

from pathlib import Path

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.governance.tool_receipts import (
    RECEIPT_KEY_ENV,
    RECEIPT_KEY_FILE_ENV,
    sign_tool_receipt,
    verify_tool_receipt,
)


KEY = "test-openclaw-receipt-key-with-at-least-32-characters"


def _receipt(*, session_id: str = "session-1", run_id: str = "run-1") -> dict:
    return {
        "receipt_version": 1,
        "attestation": "hmac-sha256",
        "source": "openclaw.after_tool_call",
        "tool_name": "pytest",
        "tool_call_id": "call-1",
        "duration_ms": 12,
        "passed": True,
        "result_digest": "a" * 64,
        "session_id": session_id,
        "run_id": run_id,
    }


def test_tool_receipt_hmac_binds_terminal_session_run_and_result(monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", KEY)
    signed = sign_tool_receipt(_receipt())

    assert verify_tool_receipt(signed, session_id="session-1", run_id="run-1") is True
    assert verify_tool_receipt(signed, session_id="other", run_id="run-1") is False
    assert verify_tool_receipt({**signed, "result_digest": "b" * 64}, session_id="session-1", run_id="run-1") is False


def test_receipt_key_file_cache_observes_provisioning_and_rotation(monkeypatch, tmp_path: Path) -> None:
    key_file = tmp_path / "evidence-receipt.env"
    first_key = "first-rotatable-receipt-key-with-32-distinct-characters-123"
    second_key = "second-rotated-receipt-key-with-32-distinct-characters-456"
    monkeypatch.delenv(RECEIPT_KEY_ENV, raising=False)
    monkeypatch.setenv(RECEIPT_KEY_FILE_ENV, str(key_file))

    assert verify_tool_receipt(_receipt(), session_id="session-1", run_id="run-1") is False
    key_file.write_text(f"{RECEIPT_KEY_ENV}={first_key}\n", encoding="utf-8")
    signed_first = sign_tool_receipt(_receipt(), key=first_key)
    assert verify_tool_receipt(signed_first, session_id="session-1", run_id="run-1") is True

    key_file.write_text(f"{RECEIPT_KEY_ENV}={second_key}\n", encoding="utf-8")
    signed_second = sign_tool_receipt(_receipt(), key=second_key)
    assert verify_tool_receipt(signed_first, session_id="session-1", run_id="run-1") is False
    assert verify_tool_receipt(signed_second, session_id="session-1", run_id="run-1") is True


def test_openclaw_hook_rejects_fabricated_unsigned_tool_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", KEY)
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    event = {
        "session_id": "session-1",
        "run_id": "run-1",
        "query": "Patch and test the module.",
        "tools": ["apply_patch", "pytest"],
        "verification": "openclaw.after_tool_call:1:pytest",
        "verification_receipts": [_receipt()],
        "outcome": {"success": True, "verified": True},
    }
    try:
        result = hooks.on_agent_end(event)
    finally:
        runtime.close()

    assert result["event"]["verification_receipts"] == []
    assert result["outcome"]["recorded"] is False
    assert result["outcome"]["reason"] == "agent_end_success_without_explicit_verification"
    assert "outcome_trace" not in result


def test_openclaw_hook_accepts_bridge_attested_tool_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", KEY)
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    event = {
        "session_id": "session-1",
        "run_id": "run-1",
        "query": "Patch and test the module.",
        "tools": ["apply_patch", "pytest"],
        "verification": "openclaw.after_tool_call:1:pytest",
        "verification_receipts": [sign_tool_receipt(_receipt())],
        "outcome": {"success": True, "verified": True},
    }
    try:
        result = hooks.on_agent_end(event)
    finally:
        runtime.close()

    assert result["event"]["verification_receipts"][0]["attestation"] == "hmac-sha256"
    assert result["outcome_trace"]["ok"] is True
