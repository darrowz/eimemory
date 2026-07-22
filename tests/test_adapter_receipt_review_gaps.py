from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from eimemory.adapters.codex.hook import CodexHookAdapter, codex_attestation_client_from_env
from eimemory.adapters.codex.mcp_server import CodexMCPServer
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.receipt_handoff import ReceiptIdHandoff
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.governance.tool_receipts import sign_tool_receipt
from eimemory.models.records import RecordEnvelope, ScopeRef


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
RECEIPT_KEY = "ReviewGapReceiptKey_0123456789-Strong"
RPC_TOKEN = "RuntimeRpcToken_0123456789-Abcdefghijklmnop"
PRODUCER_TOKEN = "ProducerReceiptToken_0123456789-Abcdefghijk"


def _seed_release(
    runtime: Runtime,
    *,
    commit: str = "d" * 40,
    version: str = "1.9.77",
) -> None:
    scope = ScopeRef.from_dict(BASE_SCOPE)
    runtime._test_runtime_commit = commit
    release_path = f"/opt/eimemory/releases/{commit}"
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Current deployment receipt",
            summary="verified",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
            content={
                "report_type": "deployment_receipt",
                "promotion_target": "code_patch",
                "action": "code_patch",
                "gate": {"ok": True, "receipt_verified": True},
                "side_effect": {
                    "ok": True,
                    "production_applied": True,
                    "deployment_executed": True,
                    "verification": {"ok": True, "skipped": False},
                    "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                    "post_deploy_health": {
                        "ok": True,
                        "skipped": False,
                        "commit": commit,
                        "version": version,
                        "release_path": release_path,
                    },
                    "commit": {"commit_sha": commit},
                    "release": {"version": version, "release_path": release_path},
                    "rollback_evidence": {
                        "prior_commit_sha": "c" * 40,
                        "rollback_command": "verified rollback",
                    },
                },
            },
            meta={
                "report_type": "deployment_receipt",
                "commit_sha": commit,
                "version": version,
                "release_path": release_path,
                "gate_ok": True,
            },
        )
    )
    assert current_release_identity(runtime, scope) is not None


def _attest(
    service: AgentRuntimeMemoryService,
    *,
    call_id: str,
    result: object | None = None,
    tool_name: str = "pytest",
    tool_input: dict | None = None,
) -> dict:
    return service.attest_tool_result(
        producer="codex",
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_input=tool_input,
        result=result if result is not None else {"exit_code": 0, "summary": "3 passed"},
    )


def _terminal(
    service: AgentRuntimeMemoryService,
    receipt_ids: list[str],
    *,
    result: str = "focused verification completed",
) -> dict:
    return service.record_terminal(
        channel="codex",
        scope=BASE_SCOPE,
        end_kind="stop",
        session_id="session-1",
        event_id="turn-1",
        task_type="code.fix",
        success=True,
        verification="caller prose is not evidence",
        result=result,
        receipt_ids=receipt_ids,
    )


def test_two_receipts_are_consumed_by_one_terminal_trace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    first = _attest(service, call_id="call-1")
    second = _attest(service, call_id="call-2")
    try:
        terminal = _terminal(service, [first["receipt_id"], second["receipt_id"]])
        rows = runtime.store.sqlite.conn.execute(
            "SELECT receipt_id, consumed_trace_id FROM adapter_tool_receipts ORDER BY receipt_id"
        ).fetchall()
    finally:
        runtime.close()

    assert terminal["ok"] is True
    assert len({str(row["consumed_trace_id"]) for row in rows}) == 1
    assert all(str(row["consumed_trace_id"]) for row in rows)


def test_legacy_unique_consumed_trace_index_is_migrated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    database = runtime.store.sqlite.path
    runtime.close()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_adapter_receipts_consumed_trace")
        conn.execute(
            "CREATE UNIQUE INDEX idx_adapter_receipts_consumed_trace ON adapter_tool_receipts(consumed_trace_id) WHERE consumed_trace_id != ''"
        )
        conn.commit()

    migrated = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(migrated)
    first = _attest(service, call_id="call-1")
    second = _attest(service, call_id="call-2")
    try:
        indexes = migrated.store.sqlite.conn.execute(
            "PRAGMA index_list(adapter_tool_receipts)"
        ).fetchall()
        target = [row for row in indexes if str(row["name"]) == "idx_adapter_receipts_consumed_trace"]
        terminal = _terminal(service, [first["receipt_id"], second["receipt_id"]])
    finally:
        migrated.close()

    assert len(target) == 1 and int(target[0]["unique"]) == 0
    assert terminal["ok"] is True


@pytest.mark.parametrize("handoff_order", ["reverse", "concurrent"])
def test_receipt_database_and_handoff_share_stable_32_item_cap(
    monkeypatch,
    tmp_path: Path,
    handoff_order: str,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    handoff_path = tmp_path / "receipt-handoff.sqlite3"
    runtime = Runtime.create(root=tmp_path / "runtime")
    service = AgentRuntimeMemoryService(runtime)
    handoff = ReceiptIdHandoff(handoff_path)
    receipt_ids: list[str] = []
    for index in range(33):
        receipt_ids.append(_attest(service, call_id=f"bounded-call-{index:02d}")["receipt_id"])

    def append_handoff(receipt_id: str) -> None:
        handoff.append(
            channel="codex",
            scope=resolve_channel_scope("codex", BASE_SCOPE),
            session_id="session-1",
            run_id="turn-1",
            receipt_id=receipt_id,
        )

    reverse_ids = list(reversed(receipt_ids))
    if handoff_order == "reverse":
        for receipt_id in reverse_ids:
            append_handoff(receipt_id)
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append_handoff, reverse_ids))
    submitted_ids = handoff.list_ids(
        channel="codex",
        scope=resolve_channel_scope("codex", BASE_SCOPE),
        session_id="session-1",
        run_id="turn-1",
    )
    try:
        claimable = runtime.store.sqlite.load_claimable_adapter_tool_receipts(
            channel="codex",
            session_id="session-1",
            run_id="turn-1",
            trace_id="",
            scope=resolve_channel_scope("codex", BASE_SCOPE),
        )
        terminal = _terminal(service, submitted_ids)
        rows = runtime.store.sqlite.conn.execute(
            "SELECT receipt_id, eligible, consumed_trace_id FROM adapter_tool_receipts ORDER BY receipt_id"
        ).fetchall()
    finally:
        runtime.close()

    stable_ids = set(sorted(receipt_ids, reverse=True)[:32])
    assert len(submitted_ids) == 32
    assert set(submitted_ids) == stable_ids
    assert {item["receipt_id"] for item in claimable} == set(submitted_ids)
    assert terminal["ok"] is True
    assert sum(int(row["eligible"]) for row in rows) == 32
    assert sum(bool(str(row["consumed_trace_id"])) for row in rows) == 32
    assert set(receipt_ids) - set(submitted_ids)


def test_old_release_receipt_is_quarantined_without_poisoning_current_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    _seed_release(runtime, commit="c" * 40, version="1.9.76")
    old = _attest(service, call_id="old-release-call")
    _seed_release(runtime, commit="d" * 40, version="1.9.77")
    current = _attest(service, call_id="current-release-call")
    try:
        terminal = _terminal(service, [old["receipt_id"], current["receipt_id"]])
        rows = {
            str(row["receipt_id"]): row
            for row in runtime.store.sqlite.conn.execute(
                "SELECT receipt_id, eligible, consumed_trace_id FROM adapter_tool_receipts"
            )
        }
    finally:
        runtime.close()

    assert terminal["ok"] is True
    assert int(rows[old["receipt_id"]]["eligible"]) == 0
    assert str(rows[old["receipt_id"]]["consumed_trace_id"]) == ""
    assert str(rows[current["receipt_id"]]["consumed_trace_id"])


def test_expired_receipt_is_quarantined_without_poisoning_fresh_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    expired = _attest(service, call_id="expired-call")
    row = runtime.store.sqlite.conn.execute(
        "SELECT receipt_json FROM adapter_tool_receipts WHERE receipt_id = ?",
        (expired["receipt_id"],),
    ).fetchone()
    expired_payload = json.loads(str(row["receipt_json"]))
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_payload["issued_at"] = (past - timedelta(minutes=5)).isoformat()
    expired_payload["expires_at"] = past.isoformat()
    expired_payload = sign_tool_receipt(expired_payload, key=RECEIPT_KEY)
    runtime.store.sqlite.conn.execute(
        "UPDATE adapter_tool_receipts SET receipt_json = ?, created_at = ? WHERE receipt_id = ?",
        (
            json.dumps(expired_payload, ensure_ascii=False, sort_keys=True),
            expired_payload["issued_at"],
            expired["receipt_id"],
        ),
    )
    runtime.store.sqlite.conn.commit()
    fresh = _attest(service, call_id="fresh-call")
    try:
        terminal = _terminal(service, [expired["receipt_id"], fresh["receipt_id"]])
        rows = {
            str(item["receipt_id"]): item
            for item in runtime.store.sqlite.conn.execute(
                "SELECT receipt_id, eligible, consumed_trace_id FROM adapter_tool_receipts"
            )
        }
    finally:
        runtime.close()

    assert terminal["ok"] is True
    assert int(rows[expired["receipt_id"]]["eligible"]) == 0
    assert str(rows[expired["receipt_id"]]["consumed_trace_id"]) == ""
    assert str(rows[fresh["receipt_id"]]["consumed_trace_id"])


def test_terminal_rejects_missing_untrusted_handoff_hint_without_consuming_pending_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    receipt = _attest(service, call_id="pending-call")
    try:
        with pytest.raises(ValueError, match="protected pending set"):
            _terminal(service, [])
        row = runtime.store.sqlite.conn.execute(
            "SELECT consumed_trace_id FROM adapter_tool_receipts WHERE receipt_id = ?",
            (receipt["receipt_id"],),
        ).fetchone()
    finally:
        runtime.close()

    assert str(row["consumed_trace_id"]) == ""


def test_same_tool_call_with_changed_result_conflicts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        _attest(service, call_id="same-call", result={"exit_code": 0, "summary": "2 passed"})
        with pytest.raises(ValueError, match="conflict"):
            _attest(service, call_id="same-call", result={"exit_code": 1, "summary": "1 failed"})
    finally:
        runtime.close()


def test_same_tool_call_with_only_secret_result_change_conflicts_without_persisting_raw_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    first_canary = "receipt-secret-alpha"
    second_canary = "receipt-secret-bravo"
    try:
        _attest(
            service,
            call_id="secret-result",
            result={"exit_code": 0, "summary": "2 passed", "refreshToken": first_canary},
        )
        with pytest.raises(ValueError, match="conflict"):
            _attest(
                service,
                call_id="secret-result",
                result={"exit_code": 0, "summary": "2 passed", "refreshToken": second_canary},
            )
        persisted = "\n".join(
            str(value)
            for row in runtime.store.sqlite.conn.iterdump()
            for value in (row,)
        )
        assert first_canary not in persisted
        assert second_canary not in persisted
    finally:
        runtime.close()


def test_same_tool_call_with_only_post_bound_invocation_change_conflicts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        _attest(
            service,
            call_id="long-invocation",
            tool_input={"command": "x" * 16_000 + "alpha"},
        )
        with pytest.raises(ValueError, match="conflict"):
            _attest(
                service,
                call_id="long-invocation",
                tool_input={"command": "x" * 16_000 + "bravo"},
            )
    finally:
        runtime.close()


def test_terminal_retry_changed_payload_or_receipt_set_rolls_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    first = _attest(service, call_id="call-1")
    try:
        original = _terminal(service, [first["receipt_id"]])
        second = _attest(service, call_id="call-2")
        with pytest.raises(ValueError, match="terminal retry conflict"):
            _terminal(
                service,
                [first["receipt_id"], second["receipt_id"]],
                result="changed terminal result",
            )
        row = runtime.store.sqlite.conn.execute(
            "SELECT consumed_trace_id FROM adapter_tool_receipts WHERE receipt_id = ?",
            (second["receipt_id"],),
        ).fetchone()
        persisted = runtime.store.sqlite.conn.execute(
            "SELECT payload_json FROM events WHERE id = ?",
            (original["event"]["id"],),
        ).fetchone()
    finally:
        runtime.close()

    assert str(row["consumed_trace_id"]) == ""
    assert json.loads(str(persisted["payload_json"])) == original["event"]


def test_terminal_retry_changed_payload_with_same_receipt_set_conflicts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    receipt = _attest(service, call_id="call-1")
    try:
        original = _terminal(service, [receipt["receipt_id"]])
        with pytest.raises(ValueError, match="terminal retry conflict"):
            _terminal(service, [receipt["receipt_id"]], result="changed terminal result")
        persisted = runtime.store.sqlite.conn.execute(
            "SELECT payload_json FROM events WHERE id = ?",
            (original["event"]["id"],),
        ).fetchone()
    finally:
        runtime.close()

    assert json.loads(str(persisted["payload_json"])) == original["event"]


def test_dashboard_requires_persisted_v2_receipt_join_for_codex(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    _seed_release(runtime)
    service = AgentRuntimeMemoryService(runtime)
    receipt = _attest(service, call_id="call-joined")
    terminal = _terminal(service, [receipt["receipt_id"]])
    scope = resolve_channel_scope("codex", BASE_SCOPE)
    before = runtime.build_capability_dashboard_metrics(scope=scope, persist=False)
    runtime.store.sqlite.conn.execute(
        "DELETE FROM adapter_tool_receipts WHERE receipt_id = ?",
        (receipt["receipt_id"],),
    )
    runtime.store.sqlite.conn.commit()
    try:
        after = runtime.build_capability_dashboard_metrics(scope=scope, persist=False)
    finally:
        runtime.close()

    assert terminal["ok"] is True
    assert before["sample_counts"]["verified_real_tasks"] == 1
    assert after["sample_counts"]["verified_real_tasks"] == 0


def test_dashboard_rejects_mixed_type_event_receipt_list(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    _seed_release(runtime)
    service = AgentRuntimeMemoryService(runtime)
    receipt = _attest(service, call_id="call-mixed-event-receipts")
    terminal = _terminal(service, [receipt["receipt_id"]])
    scope = resolve_channel_scope("codex", BASE_SCOPE)
    assert runtime.build_capability_dashboard_metrics(
        scope=scope, persist=False
    )["sample_counts"]["verified_real_tasks"] == 1

    event = dict(terminal["event"])
    event["verification_receipts"] = [*event["verification_receipts"], "not-a-receipt"]
    runtime.store.sqlite.conn.execute(
        "UPDATE events SET payload_json = ? WHERE id = ?",
        (json.dumps(event, sort_keys=True), event["id"]),
    )
    runtime.store.sqlite.conn.commit()
    try:
        after = runtime.build_capability_dashboard_metrics(scope=scope, persist=False)
    finally:
        runtime.close()

    assert after["sample_counts"]["verified_real_tasks"] == 0


def test_dashboard_fails_closed_for_non_object_persisted_receipt_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    _seed_release(runtime)
    service = AgentRuntimeMemoryService(runtime)
    receipt = _attest(service, call_id="call-non-object-persisted-receipt")
    _terminal(service, [receipt["receipt_id"]])
    scope = resolve_channel_scope("codex", BASE_SCOPE)
    assert runtime.build_capability_dashboard_metrics(
        scope=scope, persist=False
    )["sample_counts"]["verified_real_tasks"] == 1

    runtime.store.sqlite.conn.execute(
        "UPDATE adapter_tool_receipts SET receipt_json = ? WHERE receipt_id = ?",
        ("[]", receipt["receipt_id"]),
    )
    runtime.store.sqlite.conn.commit()
    try:
        after = runtime.build_capability_dashboard_metrics(scope=scope, persist=False)
    finally:
        runtime.close()

    assert after["sample_counts"]["verified_real_tasks"] == 0


def test_generic_tool_output_cannot_pass_verification_policy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        receipt = _attest(service, call_id="generic", tool_name="generic_tool")
    finally:
        runtime.close()

    assert receipt["receipt"]["passed"] is False
    assert receipt["receipt"]["verification_policy_id"] == "execution_only.v1"


def test_shell_wrapper_requires_an_anchored_test_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        generic = _attest(
            service,
            call_id="generic-shell",
            tool_name="shell_command",
            tool_input={"command": "echo 3 passed"},
        )
        verified = _attest(
            service,
            call_id="test-shell",
            tool_name="shell_command",
            tool_input={"command": "rtk pytest -- tests/test_unit.py -q"},
        )
        coverage_failure = _attest(
            service,
            call_id="test-shell-coverage-failure",
            tool_name="shell_command",
            tool_input={"command": "python -m pytest --cov --cov-fail-under=90"},
            result={
                "exit_code": 1,
                "summary": "3 passed; coverage failure below fail-under=90",
            },
        )
        plugin_error = _attest(
            service,
            call_id="test-shell-plugin-error",
            tool_name="shell_command",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result={"exit_code": 0, "summary": "3 passed", "error": "plugin failed"},
        )
    finally:
        runtime.close()

    assert generic["receipt"]["passed"] is False
    assert verified["receipt"]["passed"] is True
    assert verified["receipt"]["verification_policy_id"] == "test_command.exit_zero.positive_count.v1"
    assert coverage_failure["receipt"]["passed"] is False
    assert plugin_error["receipt"]["passed"] is False


def test_codex_bash_string_output_never_counts_without_explicit_exit_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    official_shell_output = "================ test session starts ================\n3 passed in 0.12s"
    try:
        positive_text_only = _attest(
            service,
            call_id="codex-bash-test",
            tool_name="Bash",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result=official_shell_output,
        )
        forged_echo = _attest(
            service,
            call_id="codex-bash-echo",
            tool_name="Bash",
            tool_input={"command": "echo '3 passed in 0.12s'"},
            result=official_shell_output,
        )
        collection_only = _attest(
            service,
            call_id="codex-bash-collection-only",
            tool_name="Bash",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result="================ test session starts ================\n3 tests collected",
        )
        chained_echo = _attest(
            service,
            call_id="codex-bash-chained-echo",
            tool_name="Bash",
            tool_input={
                "command": "python -m pytest tests/missing.py -q; echo '3 passed in 0.12s'"
            },
            result=official_shell_output,
        )
        coverage_gate_failure = _attest(
            service,
            call_id="codex-bash-coverage-failure",
            tool_name="Bash",
            tool_input={"command": "python -m pytest --cov --cov-fail-under=90"},
            result="3 passed in 0.12s\nERROR Coverage failure: total of 72 is less than fail-under=90",
        )
        plugin_error = _attest(
            service,
            call_id="codex-bash-plugin-error",
            tool_name="Bash",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result="3 passed in 0.12s\nERROR pytest plugin teardown failed",
        )
        configuration_error = _attest(
            service,
            call_id="codex-bash-config-error",
            tool_name="Bash",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result="3 passed in 0.12s\nERROR: invalid pytest configuration",
        )
        json_shaped_raw_output = _attest(
            service,
            call_id="codex-bash-json-shaped-raw",
            tool_name="Bash",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result=json.dumps({"output": "3 passed in 0.12s", "exit_code": 0, "error": ""}),
        )
    finally:
        runtime.close()

    for receipt in (
        positive_text_only,
        forged_echo,
        collection_only,
        chained_echo,
        coverage_gate_failure,
        plugin_error,
        configuration_error,
        json_shaped_raw_output,
    ):
        assert receipt["receipt"]["passed"] is False
        assert receipt["receipt"]["verification_policy_id"] == "execution_only.v1"


@pytest.mark.parametrize(
    ("payload", "expected_passed"),
    [
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": None},
            True,
            id="official-null-error",
        ),
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": ""},
            True,
            id="empty-string-error",
        ),
        ({"output": "3 passed in 0.12s", "exit_code": 0}, False),
        ({"output": "3 passed in 0.12s", "exit_code": 1, "error": ""}, False),
        ({"output": "3 passed in 0.12s", "exit_code": 0, "error": "plugin failed"}, False),
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": "   "},
            False,
            id="whitespace-error",
        ),
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": 0},
            False,
            id="numeric-error",
        ),
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": False},
            False,
            id="boolean-error",
        ),
        pytest.param(
            {"output": "3 passed in 0.12s", "exit_code": 0, "error": []},
            False,
            id="list-error",
        ),
    ],
)
def test_hermes_json_string_requires_complete_host_status_envelope(
    monkeypatch,
    tmp_path: Path,
    payload: dict,
    expected_passed: bool,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        result = service.attest_tool_result(
            producer="hermes",
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="session-1",
            run_id="turn-1",
            tool_call_id="call-1",
            tool_name="terminal",
            tool_input={"command": "python -m pytest tests/test_unit.py -q"},
            result=json.dumps(payload),
        )
    finally:
        runtime.close()

    assert result["receipt"]["passed"] is expected_passed


def test_codex_receipt_handoff_survives_adapter_process_boundary_and_clears_after_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "codex-receipt-handoff.sqlite3"
    monkeypatch.setenv("EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE", str(handoff))

    class AttestationClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            assert method == "adapter.attest_tool_result"
            return {
                "ok": True,
                "bypassed": False,
                "result": {
                    "receipt_id": "rcpt-codex-cross-process",
                    "receipt": {"passed": True},
                },
            }

    class TerminalClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call_or_bypass(self, method: str, params: dict) -> dict:
            self.calls.append((method, params))
            return {"ok": True, "bypassed": False, "result": {"ok": True}}

    producer_process = CodexHookAdapter(
        client=TerminalClient(),
        scope=BASE_SCOPE,
        attestation_client=AttestationClient(),
    )
    producer_process.handle(
        "PostToolUse",
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
            "tool_name": "pytest",
            "tool_response": {"exit_code": 0, "summary": "1 passed"},
        },
    )

    terminal_client = TerminalClient()
    terminal_process = CodexHookAdapter(client=terminal_client, scope=BASE_SCOPE)
    terminal_process.handle(
        "Stop",
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "task_type": "code.fix",
            "success": True,
            "result": "done",
        },
    )
    terminal_params = [params for method, params in terminal_client.calls if method == "adapter.record_terminal"][0]

    assert terminal_params["receipt_ids"] == ["rcpt-codex-cross-process"]
    assert handoff.exists()
    with sqlite3.connect(handoff) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receipt_handoff").fetchone()[0] == 0
    persisted = handoff.read_bytes()
    assert PRODUCER_TOKEN.encode() not in persisted


def test_codex_receipt_handoff_is_retained_after_terminal_failure(monkeypatch, tmp_path: Path) -> None:
    handoff_path = tmp_path / "codex-receipt-handoff.sqlite3"
    monkeypatch.setenv("EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE", str(handoff_path))
    handoff = ReceiptIdHandoff(handoff_path)
    handoff.append(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
        receipt_id="rcpt-codex-retained",
    )

    class FailedTerminalClient:
        def call_or_bypass(self, method: str, params: dict) -> dict:
            assert method == "adapter.record_terminal"
            return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}

    CodexHookAdapter(client=FailedTerminalClient(), scope=BASE_SCOPE).handle(
        "Stop",
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "task_type": "code.fix",
            "success": True,
            "result": "done",
        },
    )

    assert handoff.list_ids(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
    ) == ["rcpt-codex-retained"]


def test_codex_mcp_terminal_submits_and_clears_handoff_receipts(monkeypatch, tmp_path: Path) -> None:
    handoff_path = tmp_path / "codex-receipt-handoff.sqlite3"
    monkeypatch.setenv("EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE", str(handoff_path))
    handoff = ReceiptIdHandoff(handoff_path)
    handoff.append(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
        receipt_id="rcpt-codex-mcp",
    )

    class TerminalClient:
        def __init__(self) -> None:
            self.params: dict = {}

        def call_or_bypass(self, method: str, params: dict) -> dict:
            assert method == "adapter.record_terminal"
            self.params = params
            return {"ok": True, "bypassed": False, "result": {"ok": True}}

    client = TerminalClient()
    server = CodexMCPServer(client=client, scope=BASE_SCOPE)
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "eimemory_verify_outcome",
                "arguments": {
                    "session_id": "session-1",
                    "event_id": "turn-1",
                    "task_type": "code.fix",
                    "success": True,
                    "verification": "diagnostic",
                    "result": "done",
                },
            },
        }
    )

    assert response["result"]["isError"] is False
    assert client.params["receipt_ids"] == ["rcpt-codex-mcp"]
    assert handoff.list_ids(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
    ) == []


def test_producer_credential_is_file_only_and_distinct_from_runtime_token(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "codex-producer.token"
    token_file.write_text(PRODUCER_TOKEN + "\n", encoding="utf-8")
    if os.name == "posix":
        token_file.chmod(0o600)
    monkeypatch.setenv("EIMEMORY_RPC_TOKEN", RPC_TOKEN)
    monkeypatch.setenv("EIMEMORY_ATTESTATION_TOKEN", PRODUCER_TOKEN)
    monkeypatch.delenv("EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE", raising=False)

    assert codex_attestation_client_from_env() is None

    monkeypatch.setenv("EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE", str(token_file))
    assert codex_attestation_client_from_env() is None

    monkeypatch.setenv("EIMEMORY_ATTESTATION_HOST_PROFILE", "same-uid-default")
    assert codex_attestation_client_from_env() is None

    monkeypatch.setenv("EIMEMORY_ATTESTATION_HOST_PROFILE", "operator-separated-v1")
    client = codex_attestation_client_from_env()
    assert client is not None
    assert client.auth_token == PRODUCER_TOKEN
    assert client.auth_token != RPC_TOKEN

    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with pytest.raises(ValueError, match="distinct"):
            EIBrainRPCServer(
                runtime,
                host="127.0.0.1",
                port=0,
                auth_token=RPC_TOKEN,
                attestation_tokens={RPC_TOKEN: "codex"},
            )
    finally:
        runtime.close()


def test_rpc_server_attestation_profile_is_private_file_only(monkeypatch, tmp_path: Path) -> None:
    registry = tmp_path / "attestation-producers.json"
    registry.write_text(json.dumps({"codex": PRODUCER_TOKEN}), encoding="utf-8")
    if os.name == "posix":
        registry.chmod(0o600)
    monkeypatch.setenv("EIMEMORY_ATTESTATION_TOKENS_JSON", json.dumps({PRODUCER_TOKEN: "codex"}))
    monkeypatch.setenv("EIMEMORY_ATTESTATION_TOKENS_FILE", str(registry))
    monkeypatch.delenv("EIMEMORY_ATTESTATION_HOST_PROFILE", raising=False)

    first_runtime = Runtime.create(root=tmp_path / "first")
    first_server = EIBrainRPCServer(
        first_runtime,
        host="127.0.0.1",
        port=0,
        auth_token=RPC_TOKEN,
    )
    first_server.start()
    try:
        status = AgentRuntimeMemoryService(first_runtime).status(channel="codex", scope=BASE_SCOPE)
        assert first_server.attestation_tokens == {}
        assert status["attestation_available"] is False
        assert status["attestation_reason"] == "operator_separated_attestation_profile_not_configured"
    finally:
        first_server.stop()
        first_runtime.close()

    monkeypatch.setenv("EIMEMORY_ATTESTATION_HOST_PROFILE", "operator-separated-v1")
    second_runtime = Runtime.create(root=tmp_path / "second")
    second_server = EIBrainRPCServer(
        second_runtime,
        host="127.0.0.1",
        port=0,
        auth_token=RPC_TOKEN,
    )
    second_server.start()
    try:
        status = AgentRuntimeMemoryService(second_runtime).status(channel="codex", scope=BASE_SCOPE)
        assert second_server.attestation_tokens == {PRODUCER_TOKEN: "codex"}
        assert status["attestation_available"] is True
        assert status["attestation_reason"] == "operator_separated_profile_active"
    finally:
        second_server.stop()
        second_runtime.close()


def _subprocess_env(tmp_path: Path, server: EIBrainRPCServer) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "EIMEMORY_ATTESTATION_TOKEN",
        "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY",
        "EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE",
        "EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE",
    ):
        env.pop(name, None)
    env.update(
        {
            "EIMEMORY_RPC_URL": f"http://{server.address[0]}:{server.address[1]}/",
            "EIMEMORY_RPC_TOKEN": RPC_TOKEN,
            "EIMEMORY_TENANT_ID": BASE_SCOPE["tenant_id"],
            "EIMEMORY_AGENT_ID": BASE_SCOPE["agent_id"],
            "EIMEMORY_WORKSPACE_ID": BASE_SCOPE["workspace_id"],
            "EIMEMORY_USER_ID": BASE_SCOPE["user_id"],
            "EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE": str(tmp_path / "receipt-handoff.sqlite3"),
        }
    )
    return env


def test_codex_post_tool_and_stop_separate_processes_preserve_exact_receipts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    producer_file = tmp_path / "codex-producer.token"
    producer_file.write_text(PRODUCER_TOKEN + "\n", encoding="utf-8")
    if os.name == "posix":
        producer_file.chmod(0o600)
    runtime = Runtime.create(root=tmp_path / "runtime")
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        auth_token=RPC_TOKEN,
        attestation_tokens={PRODUCER_TOKEN: "codex"},
    )
    server.start()
    env = _subprocess_env(tmp_path, server)
    post_env = {**env, "EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE": str(producer_file)}
    post_env["EIMEMORY_ATTESTATION_HOST_PROFILE"] = "operator-separated-v1"
    post = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(Path(__file__).parents[1]),
        "hook_event_name": "PostToolUse",
        "permission_mode": "default",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-1",
        "tool_use_id": "call-1",
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tests/test_unit.py -q"},
        "tool_response": "================ test session starts ================\n2 passed in 0.12s",
    }
    stop = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(Path(__file__).parents[1]),
        "hook_event_name": "Stop",
        "permission_mode": "default",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-1",
        "last_assistant_message": "done",
    }
    try:
        first = subprocess.run(
            [sys.executable, "-m", "eimemory.adapters.codex.hook", "--event", "PostToolUse"],
            input=json.dumps(post),
            text=True,
            capture_output=True,
            cwd=Path(__file__).parents[1],
            env=post_env,
            timeout=20,
            check=False,
        )
        second = subprocess.run(
            [sys.executable, "-m", "eimemory.adapters.codex.hook", "--event", "Stop"],
            input=json.dumps(stop),
            text=True,
            capture_output=True,
            cwd=Path(__file__).parents[1],
            env=env,
            timeout=20,
            check=False,
        )
        row = runtime.store.sqlite.conn.execute(
            "SELECT receipt_json, consumed_trace_id FROM adapter_tool_receipts"
        ).fetchone()
        event_row = runtime.store.sqlite.conn.execute(
            "SELECT payload_json FROM events WHERE source = 'codex.stop'"
        ).fetchone()
    finally:
        server.stop()
        runtime.close()

    assert (first.returncode, second.returncode) == (0, 0)
    assert row is not None and str(row["consumed_trace_id"]) == ""
    receipt = json.loads(str(row["receipt_json"]))
    assert receipt["source"] == "codex.post_tool_use"
    assert receipt["passed"] is False
    assert receipt["verification_policy_id"] == "execution_only.v1"
    event = json.loads(str(event_row["payload_json"]))
    assert event["outcome_trace_task_type"] == "code.unverified"
    assert event["verification"] == ""
    assert event["evidence_class"] == "diagnostic_task"
    assert PRODUCER_TOKEN not in env.values()


def test_hermes_official_terminal_lifecycle_binds_verified_host_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    producer_file = tmp_path / "hermes-producer.token"
    producer_file.write_text(PRODUCER_TOKEN + "\n", encoding="utf-8")
    if os.name == "posix":
        producer_file.chmod(0o600)
    runtime = Runtime.create(root=tmp_path / "runtime")
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        auth_token=RPC_TOKEN,
        attestation_tokens={PRODUCER_TOKEN: "hermes"},
    )
    server.start()
    env = _subprocess_env(tmp_path, server)
    post_env = {**env, "EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE": str(producer_file)}
    post_env["EIMEMORY_ATTESTATION_HOST_PROFILE"] = "operator-separated-v1"
    lifecycle_script = """
import json
from integrations.hermes.eimemory import register
class Context:
    def register_memory_provider(self, provider): self.provider = provider
    def register_hook(self, name, callback): self.callback = callback
ctx = Context()
register(ctx)
ctx.provider.initialize('session-1', agent_identity='hongtu', agent_workspace='embodied', user_id='darrow')
ctx.callback('terminal', {'command': 'python -m pytest tests/test_unit.py -q'}, json.dumps({'output':'2 passed in 0.12s','exit_code':0,'error':None}), 'task-1', 10, session_id='session-1', turn_id='turn-1', tool_call_id='call-1')
good = json.loads(ctx.provider.handle_tool_call('eimemory_verify_outcome', {'result':'done'}))
assert good['ok'] is True
ctx.callback('terminal', {'command': 'python -m pytest tests/test_a.py -q'}, json.dumps({'output':'1 passed in 0.10s','exit_code':0,'error':None}), 'task-2', 10, session_id='session-1', turn_id='turn-2', tool_call_id='call-2')
ctx.callback('terminal', {'command': 'python -m pytest tests/test_b.py -q'}, json.dumps({'output':'1 passed in 0.10s','exit_code':0,'error':None}), 'task-3', 10, session_id='session-1', turn_id='turn-3', tool_call_id='call-3')
ambiguous = json.loads(ctx.provider.handle_tool_call('eimemory_verify_outcome', {'result':'done'}))
assert ambiguous['ok'] is False
"""
    try:
        process = subprocess.run(
            [sys.executable, "-c", lifecycle_script],
            text=True,
            capture_output=True,
            cwd=Path(__file__).parents[1],
            env=post_env,
            timeout=20,
            check=False,
        )
        row = runtime.store.sqlite.conn.execute(
            "SELECT receipt_json, consumed_trace_id FROM adapter_tool_receipts WHERE consumed_trace_id != ''"
        ).fetchone()
    finally:
        server.stop()
        runtime.close()

    assert process.returncode == 0, process.stderr
    assert row is not None and str(row["consumed_trace_id"])
    assert json.loads(str(row["receipt_json"]))["source"] == "hermes.post_tool_call"


def test_status_reports_fail_closed_attestation_profile(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        auth_token=RPC_TOKEN,
        attestation_tokens={},
    )
    server.start()
    try:
        result = AgentRuntimeMemoryService(runtime).status(channel="codex", scope=BASE_SCOPE)
    finally:
        server.stop()
        runtime.close()

    assert result["attestation_available"] is False
    assert result["attestation_reason"] == "operator_separated_attestation_profile_not_configured"
