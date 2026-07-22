from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime
import eimemory.governance.tool_receipts as receipt_module
from eimemory.governance.tool_receipts import (
    RECEIPT_KEY_ENV,
    RECEIPT_KEY_FILE_ENV,
    sign_tool_receipt,
    tool_receipt_commitment,
    verify_tool_receipt,
)


KEY = "test-openclaw-receipt-key-with-at-least-32-characters"
PREVIOUS_KEY = "previous-openclaw-receipt-key-with-at-least-32-characters"
KEY_ID = "key_" + sha256(KEY.encode("utf-8")).hexdigest()[:16]
PREVIOUS_KEY_ID = "key_" + sha256(PREVIOUS_KEY.encode("utf-8")).hexdigest()[:16]


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


def test_tool_receipt_commitment_rejects_arbitrary_object_representation(monkeypatch) -> None:
    monkeypatch.setenv(RECEIPT_KEY_ENV, KEY)

    class AmbiguousObject:
        def __str__(self) -> str:
            return "shared-representation"

    with pytest.raises(TypeError, match="unsupported tool receipt commitment value type"):
        tool_receipt_commitment(AmbiguousObject(), domain="result")


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


def test_v2_keyring_signs_with_active_id_and_verifies_previous_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert hasattr(receipt_module, "RECEIPT_KEYRING_FILE_ENV")
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    keyring = tmp_path / "receipt-keyring.json"
    keyring.write_text(
        json.dumps(
            {
                "active": {"key_id": KEY_ID, "key": KEY},
                "previous": [{"key_id": PREVIOUS_KEY_ID, "key": PREVIOUS_KEY}],
            }
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        keyring.chmod(0o600)
    monkeypatch.delenv(RECEIPT_KEY_ENV, raising=False)
    monkeypatch.delenv(RECEIPT_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(receipt_module.RECEIPT_KEYRING_FILE_ENV, str(keyring))

    current = sign_tool_receipt(_v2_receipt(now=now))
    previous = sign_tool_receipt(
        _v2_receipt(now=now, key_id=PREVIOUS_KEY_ID),
        key=PREVIOUS_KEY,
        key_id=PREVIOUS_KEY_ID,
    )
    historical_v1 = sign_tool_receipt(_receipt(), key=PREVIOUS_KEY)

    assert current["key_id"] == KEY_ID
    assert verify_tool_receipt(
        current, session_id="session-1", run_id="run-1", now=now,
    ) is True
    assert verify_tool_receipt(
        previous, session_id="session-1", run_id="run-1", now=now,
    ) is True
    assert verify_tool_receipt(
        historical_v1, session_id="session-1", run_id="run-1",
    ) is True


def test_v2_keyring_rejects_alias_key_ids_and_uses_key_fingerprints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    keyring = tmp_path / "receipt-keyring.json"
    keyring.write_text(
        json.dumps({"active": {"key_id": "friendly-alias", "key": KEY}, "previous": []}),
        encoding="utf-8",
    )
    if os.name == "posix":
        keyring.chmod(0o600)
    monkeypatch.delenv(RECEIPT_KEY_ENV, raising=False)
    monkeypatch.delenv(RECEIPT_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(receipt_module.RECEIPT_KEYRING_FILE_ENV, str(keyring))

    with pytest.raises(ValueError, match="attestation key is unavailable"):
        sign_tool_receipt(_v2_receipt(now=now))

    fingerprint = "key_" + sha256(KEY.encode("utf-8")).hexdigest()[:16]
    keyring.write_text(
        json.dumps({"active": {"key_id": fingerprint, "key": KEY}, "previous": []}),
        encoding="utf-8",
    )
    if os.name == "posix":
        keyring.chmod(0o600)
    signed = sign_tool_receipt(_v2_receipt(now=now))

    assert signed["key_id"] == fingerprint


def test_v2_direct_sign_rejects_key_id_alias() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="must match the key fingerprint"):
        sign_tool_receipt(
            _v2_receipt(now=now, key_id="friendly-alias"),
            key=KEY,
            key_id="friendly-alias",
        )

    signed = sign_tool_receipt(
        _v2_receipt(now=now, key_id=KEY_ID),
        key=KEY,
        key_id=KEY_ID,
    )
    assert signed["key_id"] == KEY_ID


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"active": {"key_id": "dup", "key": KEY}, "previous": [{"key_id": "dup", "key": PREVIOUS_KEY}]}),
        json.dumps({"active": {"key_id": "current", "key": KEY}, "previous": [{"key_id": f"old-{i}", "key": f"PreviousReceiptKey_{i}_abcdefghijklmnopqrstuvwxyz0123456789"} for i in range(5)]}),
        "x" * 20_000,
    ],
)
def test_v2_keyring_rejects_malformed_duplicate_over_cap_and_oversized_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
) -> None:
    assert hasattr(receipt_module, "RECEIPT_KEYRING_FILE_ENV")
    keyring = tmp_path / "receipt-keyring.json"
    keyring.write_text(payload, encoding="utf-8")
    if os.name == "posix":
        keyring.chmod(0o600)
    monkeypatch.delenv(RECEIPT_KEY_ENV, raising=False)
    monkeypatch.delenv(RECEIPT_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(receipt_module.RECEIPT_KEYRING_FILE_ENV, str(keyring))

    with pytest.raises(ValueError, match="attestation key is unavailable"):
        sign_tool_receipt(_v2_receipt(now=datetime.now(timezone.utc)))


def test_v2_keyring_rejects_symlink_and_posix_permissive_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert hasattr(receipt_module, "RECEIPT_KEYRING_FILE_ENV")
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps({"active": {"key_id": "current", "key": KEY}, "previous": []}),
        encoding="utf-8",
    )
    if os.name == "posix":
        target.chmod(0o600)
    link = tmp_path / "keyring-link.json"
    try:
        link.symlink_to(target)
    except OSError:
        link = target
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self == link or original_is_symlink(self),
        )
    monkeypatch.delenv(RECEIPT_KEY_ENV, raising=False)
    monkeypatch.delenv(RECEIPT_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(receipt_module.RECEIPT_KEYRING_FILE_ENV, str(link))
    with pytest.raises(ValueError, match="attestation key is unavailable"):
        sign_tool_receipt(_v2_receipt(now=datetime.now(timezone.utc)))

    assert receipt_module._secure_file_mode(0o100644, platform_name="posix") is False


def test_v2_receipt_enforces_issued_at_expiry_future_and_server_maximum() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    signed = sign_tool_receipt(_v2_receipt(now=now), key=KEY, key_id=KEY_ID)
    overlong = sign_tool_receipt(
        {**_v2_receipt(now=now), "expires_at": (now + timedelta(minutes=16)).isoformat()},
        key=KEY,
        key_id=KEY_ID,
    )

    assert verify_tool_receipt(
        signed, session_id="session-1", run_id="run-1", key=KEY, now=now,
    ) is True
    assert verify_tool_receipt(
        signed, session_id="session-1", run_id="run-1", key=KEY,
        now=now + timedelta(minutes=15),
    ) is False
    assert verify_tool_receipt(
        signed, session_id="session-1", run_id="run-1", key=KEY,
        now=now - timedelta(microseconds=1),
    ) is False
    assert verify_tool_receipt(
        overlong, session_id="session-1", run_id="run-1", key=KEY, now=now,
        max_age_seconds=15 * 60,
    ) is False


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


def test_v2_attestation_credential_is_mapped_to_one_producer_and_computes_policy(monkeypatch, tmp_path) -> None:
    """A normal RPC caller cannot mint Codex verification evidence."""
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setenv(RECEIPT_KEY_ENV, KEY)
    bridge = EIBrainRPCBridge(runtime)
    request = {
        "method": "adapter.attest_tool_result",
        "params": {
            "channel": "codex",
            "scope": {"agent_id": "codex", "workspace_id": "project", "user_id": "user"},
            "session_id": "session-1",
            "run_id": "turn-1",
            "tool_call_id": "call-1",
            "tool_name": "pytest",
            "result": {"exit_code": 0, "summary": "5 passed in 0.12s"},
            "passed": True,
        },
    }


def _v2_receipt(*, now: datetime, key_id: str = KEY_ID) -> dict:
    return {
        "receipt_version": 2,
        "attestation": "hmac-sha256-v2",
        "attestation_id": "rcpt-codex-1",
        "receipt_id": "rcpt-codex-1",
        "channel": "codex",
        "source": "codex.post_tool_use",
        "tool_name": "pytest",
        "tool_call_id": "call-1",
        "duration_ms": 12,
        "passed": True,
        "invocation_digest": "c" * 64,
        "result_digest": "a" * 64,
        "verification_policy_id": "test_command.exit_zero.positive_count.v1",
        "retrieval_policy_digest": "b" * 64,
        "session_id": "session-1",
        "run_id": "run-1",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "key_id": key_id,
    }
    try:
        denied = bridge.handle(request)
        minted = bridge.handle(request, attestation_producer="codex")
    finally:
        runtime.close()

    assert denied["ok"] is False
    assert denied["error"] == "attestation_unauthorized"
    assert minted["ok"] is True
    assert minted["result"]["receipt_id"].startswith("rcpt_codex_")
    assert minted["result"]["receipt"]["receipt_version"] == 2
    assert minted["result"]["receipt"]["passed"] is True
    assert minted["result"]["receipt"]["verification_policy_id"] == "test_command.exit_zero.positive_count.v1"


def test_v2_attestation_rejects_caller_selected_success_and_echo(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setenv(RECEIPT_KEY_ENV, KEY)
    bridge = EIBrainRPCBridge(runtime)
    request = {
        "method": "adapter.attest_tool_result",
        "params": {
            "channel": "codex",
            "scope": {"agent_id": "codex", "workspace_id": "project", "user_id": "user"},
            "session_id": "session-1",
            "run_id": "turn-1",
            "tool_call_id": "call-echo",
            "tool_name": "echo",
            "result": {"exit_code": 0, "summary": "passed=true"},
            "passed": True,
        },
    }
    try:
        minted = bridge.handle(request, attestation_producer="codex")
    finally:
        runtime.close()

    assert minted["ok"] is True
    assert minted["result"]["receipt"]["passed"] is False
    assert minted["result"]["receipt"]["verification_policy_id"] == "execution_only.v1"
