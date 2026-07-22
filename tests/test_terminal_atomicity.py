from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
import eimemory.experience.outcome as outcome_module
from eimemory.models.records import ScopeRef


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
RECEIPT_KEY = "TerminalAtomicityReceiptKey_0123456789-Strong"


def _service(root: Path) -> tuple[Runtime, AgentRuntimeMemoryService]:
    runtime = Runtime.create(root=root)
    return runtime, AgentRuntimeMemoryService(runtime)


def _attest(service: AgentRuntimeMemoryService, *, call_id: str = "call-1") -> dict[str, Any]:
    return service.attest_tool_result(
        producer="codex",
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        run_id="turn-1",
        tool_call_id=call_id,
        tool_name="pytest",
        result={"exit_code": 0, "summary": "3 passed"},
    )


def _terminal(service: AgentRuntimeMemoryService, receipt_id: str) -> dict[str, Any]:
    return service.record_terminal(
        channel="codex",
        scope=BASE_SCOPE,
        end_kind="stop",
        session_id="session-1",
        event_id="turn-1",
        task_type="code.fix",
        success=True,
        verification="caller text is not evidence",
        result="focused verification completed",
        receipt_ids=[receipt_id],
    )


def _effect_counts(runtime: Runtime) -> dict[str, int]:
    conn = runtime.store.sqlite.conn
    return {
        "consumed": int(
            conn.execute(
                "SELECT COUNT(*) FROM adapter_tool_receipts WHERE consumed_trace_id != ''"
            ).fetchone()[0]
        ),
        "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        "outcomes": int(conn.execute("SELECT COUNT(*) FROM event_outcomes").fetchone()[0]),
        "traces": int(
            conn.execute(
                "SELECT COUNT(*) FROM records WHERE source = 'eimemory.experience.outcome_trace'"
            ).fetchone()[0]
        ),
        "outbox": int(conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0]),
    }


def _all_sqlite_text(runtime: Runtime) -> str:
    conn = runtime.store.sqlite.conn
    material: list[str] = []
    for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        table = str(table_row[0])
        if not table.replace("_", "").isalnum():
            continue
        columns = [
            str(column[1])
            for column in conn.execute(f"PRAGMA table_info({table})")
            if "TEXT" in str(column[2]).upper()
        ]
        if columns:
            quoted = ", ".join(f'"{column}"' for column in columns)
            material.extend(
                str(value or "")
                for db_row in conn.execute(f'SELECT {quoted} FROM "{table}"')
                for value in db_row
            )
    return "\n".join(material)


@pytest.mark.parametrize("fault_stage", ["after_event", "after_outcome"])
def test_terminal_bundle_rolls_back_every_effect_and_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault_stage: str,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    sqlite = runtime.store.sqlite
    original_record_outcome = sqlite.record_outcome
    original_upsert = sqlite.upsert

    if fault_stage == "after_event":
        def fail_after_event(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("fault after event")

        monkeypatch.setattr(sqlite, "record_outcome", fail_after_event)
    else:
        def fail_after_outcome(record: Any, *args: Any, **kwargs: Any) -> None:
            original_upsert(record, *args, **kwargs)
            if record.source == "eimemory.experience.outcome_trace":
                raise RuntimeError("fault after outcome")

        monkeypatch.setattr(sqlite, "upsert", fail_after_outcome)

    try:
        with pytest.raises(RuntimeError, match=fault_stage.replace("_", " ")):
            _terminal(service, receipt["receipt_id"])
        assert _effect_counts(runtime) == {
            "consumed": 0,
            "events": 0,
            "outcomes": 0,
            "traces": 0,
            "outbox": 0,
        }

        monkeypatch.setattr(sqlite, "record_outcome", original_record_outcome)
        monkeypatch.setattr(sqlite, "upsert", original_upsert)
        retried = _terminal(service, receipt["receipt_id"])
        assert retried["ok"] is True
        assert _effect_counts(runtime) == {
            "consumed": 1,
            "events": 1,
            "outcomes": 1,
            "traces": 1,
            "outbox": 3,
        }
    finally:
        runtime.close()


def test_outcome_trace_builder_is_pure_and_deterministic() -> None:
    assert hasattr(outcome_module, "build_outcome_trace_record"), (
        "outcome.py must expose the terminal-safe pure record builder"
    )
    build = outcome_module.build_outcome_trace_record
    payload = {
        "source": "codex.stop",
        "trace_id": "trace-codex-deterministic",
        "idempotency_key": "codex.stop:session-1:turn-1",
        "task_type": "code.fix",
        "recorded_at": "2026-07-21T12:00:00+00:00",
        "input_summary": "verified",
        "selected_tools": [],
        "actions": [],
        "outcome": {"status": "good", "success": True, "rehearsal": False},
    }
    scope = ScopeRef.from_dict(resolve_channel_scope("codex", BASE_SCOPE))

    first = build(payload, scope=scope)
    second = build(dict(payload), scope=scope)

    assert first.record.record_id == second.record.record_id
    assert first.record.to_dict() == second.record.to_dict()
    assert first.payload == second.payload


def test_same_receipt_same_trace_is_idempotent_and_different_trace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    scope = resolve_channel_scope("codex", BASE_SCOPE)
    sqlite = runtime.store.sqlite
    try:
        first = sqlite.consume_adapter_tool_receipts(
            [receipt["receipt_id"]], channel="codex", session_id="session-1",
            run_id="turn-1", trace_id="trace-a", scope=scope,
        )
        same = sqlite.consume_adapter_tool_receipts(
            [receipt["receipt_id"]], channel="codex", session_id="session-1",
            run_id="turn-1", trace_id="trace-a", scope=scope,
        )
        with pytest.raises(ValueError, match="different terminal trace"):
            sqlite.consume_adapter_tool_receipts(
                [receipt["receipt_id"]], channel="codex", session_id="session-1",
                run_id="turn-1", trace_id="trace-b", scope=scope,
            )
    finally:
        runtime.close()

    assert [item["receipt_id"] for item in first] == [receipt["receipt_id"]]
    assert same == first


def test_same_terminal_retry_returns_the_original_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    try:
        first = _terminal(service, receipt["receipt_id"])
        first_trace = runtime.store.get_by_id(
            first["outcome_trace"]["record_id"],
            scope=resolve_channel_scope("codex", BASE_SCOPE),
        )
        second = _terminal(service, receipt["receipt_id"])
        second_trace = runtime.store.get_by_id(
            second["outcome_trace"]["record_id"],
            scope=resolve_channel_scope("codex", BASE_SCOPE),
        )
    finally:
        runtime.close()

    assert first["event"] == second["event"]
    assert first["outcome"] == second["outcome"]
    assert first_trace is not None and second_trace is not None
    assert first_trace.to_dict() == second_trace.to_dict()


def test_two_connections_racing_remember_and_terminal_create_one_authoritative_row_each(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    first_runtime, first_service = _service(tmp_path)
    second_runtime, second_service = _service(tmp_path)
    services = [first_service, second_service]
    remember_barrier = Barrier(2)
    originals = [service.runtime.memory.ingest for service in services]
    for service, original in zip(services, originals, strict=True):
        def synchronized_ingest(*args: Any, _original=original, **kwargs: Any) -> Any:
            remember_barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        monkeypatch.setattr(service.runtime.memory, "ingest", synchronized_ingest)

    def remember(service: AgentRuntimeMemoryService) -> dict[str, Any]:
        return service.remember(
            channel="codex", scope=BASE_SCOPE, event_id="remember-race-1",
            text="Always bind terminal evidence to one atomic transaction.",
            memory_type="durable_fact", force_capture=True,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            memories = list(pool.map(remember, services))
        memory_ids = {item["record"]["record_id"] for item in memories}
        conn = first_runtime.store.sqlite.conn
        assert len(memory_ids) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM records WHERE kind = 'memory' AND source = 'codex.memory'"
        ).fetchone()[0] == 1

        receipt = _attest(first_service, call_id="terminal-race-call")
        terminal_barrier = Barrier(2)

        def terminal(service: AgentRuntimeMemoryService) -> dict[str, Any]:
            terminal_barrier.wait(timeout=5)
            return _terminal(service, receipt["receipt_id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            terminals = list(pool.map(terminal, services))
        assert {item["event"]["id"] for item in terminals} == {terminals[0]["event"]["id"]}
        assert {item["outcome"]["id"] for item in terminals} == {terminals[0]["outcome"]["id"]}
        assert {item["outcome_trace"]["record_id"] for item in terminals} == {
            terminals[0]["outcome_trace"]["record_id"]
        }
        assert _effect_counts(first_runtime) == {
            "consumed": 1,
            "events": 1,
            "outcomes": 1,
            "traces": 1,
            "outbox": 4,
        }
    finally:
        second_runtime.close()
        first_runtime.close()


def test_terminal_structural_redaction_and_receipt_allowlist_leave_no_sqlite_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    conn = runtime.store.sqlite.conn
    row = conn.execute(
        "SELECT receipt_json FROM adapter_tool_receipts WHERE receipt_id = ?",
        (receipt["receipt_id"],),
    ).fetchone()
    decorated = json.loads(str(row["receipt_json"]))
    decorated["arbitrary_nested"] = {"private": ["receipt-secret-value"]}
    conn.execute("DELETE FROM adapter_tool_receipts WHERE receipt_id = ?", (receipt["receipt_id"],))
    conn.commit()
    runtime.store.sqlite.register_adapter_tool_receipt(
        decorated,
        scope=resolve_channel_scope("codex", BASE_SCOPE),
    )
    try:
        result = service.record_terminal(
            channel="codex", scope=BASE_SCOPE, end_kind="stop",
            session_id="session-1", event_id="turn-1", task_type="code.fix",
            success=True,
            verification={
                "authorization": "Bearer terminal-auth-secret",
                "checks": ["token=terminal-token-secret", {"ok": True}],
            },  # type: ignore[arg-type]
            result=[
                "password=terminal-password-secret",
                {"cookie": "terminal-cookie-secret", "summary": "3 passed"},
            ],  # type: ignore[arg-type]
            receipt_ids=[receipt["receipt_id"]],
        )
        assert result["ok"] is True

        persisted = _all_sqlite_text(runtime)
        for secret in (
            "terminal-auth-secret", "terminal-token-secret",
            "terminal-password-secret", "terminal-cookie-secret",
            "receipt-secret-value", "arbitrary_nested",
        ):
            assert secret not in persisted
    finally:
        runtime.close()


def test_terminal_serialized_json_camel_case_secrets_leave_no_sqlite_canary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    canaries = (
        "refresh-token-canary",
        "access-token-canary",
        "client-secret-canary",
        "session-cookie-canary",
    )
    serialized = json.dumps(
        {
            "refreshToken": canaries[0],
            "accessToken": canaries[1],
            "clientSecret": canaries[2],
            "sessionCookies": [canaries[3]],
            "summary": "safe diagnostic",
        }
    )
    try:
        result = service.record_terminal(
            channel="codex", scope=BASE_SCOPE, end_kind="stop",
            session_id="session-1", event_id="turn-1", task_type="code.fix",
            success=True, verification="caller prose is diagnostic",
            result=serialized, receipt_ids=[receipt["receipt_id"]],
        )
        assert result["ok"] is True
        persisted = _all_sqlite_text(runtime)
        assert "safe diagnostic" in persisted
        for canary in canaries:
            assert canary not in persisted
    finally:
        runtime.close()


def test_terminal_embedded_quoted_json_secret_leaves_no_event_outcome_or_trace_canary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    runtime, service = _service(tmp_path)
    receipt = _attest(service)
    canary = "embedded-refresh-token-canary"
    embedded_log = (
        'prefix payload={"refreshToken":"'
        + canary
        + '","summary":"safe embedded diagnostic"} suffix'
    )
    try:
        terminal = service.record_terminal(
            channel="codex", scope=BASE_SCOPE, end_kind="stop",
            session_id="session-1", event_id="turn-1", task_type="code.fix",
            success=True, verification=embedded_log, result=embedded_log,
            receipt_ids=[receipt["receipt_id"]],
        )
        assert terminal["ok"] is True
        assert canary not in json.dumps(terminal, ensure_ascii=False)
        assert canary not in _all_sqlite_text(runtime)
        assert "safe embedded diagnostic" in _all_sqlite_text(runtime)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("field", "sensitive_text", "secret_fragments", "expected_safe_text"),
    [
        (
            "result",
            'prefix password="quoted-head-alpha quoted-tail-bravo quoted-tail-charlie"; detail=safe\nnext line stays',
            ("quoted-head-alpha", "quoted-tail-bravo", "quoted-tail-charlie"),
            "prefix [REDACTED]; detail=safe\nnext line stays",
        ),
        (
            "verification",
            "check Authorization: Bearer bearer-head-alpha bearer-tail-bravo bearer-tail-charlie; detail=safe\nnext line stays",
            ("bearer-head-alpha", "bearer-tail-bravo", "bearer-tail-charlie"),
            "check [REDACTED]; detail=safe\nnext line stays",
        ),
        (
            "result",
            "check Bearer standalone-head-alpha standalone-tail-bravo standalone-tail-charlie; detail=safe\nnext line stays",
            ("standalone-head-alpha", "standalone-tail-bravo", "standalone-tail-charlie"),
            "check Bearer [REDACTED]; detail=safe\nnext line stays",
        ),
        pytest.param(
            "result",
            "prefix cookie=cookie-head-alpha cookie-tail-bravo cookie-tail-charlie; detail=safe\nnext line stays",
            ("cookie-head-alpha", "cookie-tail-bravo", "cookie-tail-charlie"),
            "prefix [REDACTED]; detail=safe\nnext line stays",
            id="unquoted-cookie-to-semicolon",
        ),
        pytest.param(
            "result",
            "cookie=cookie-field-head cookie-field-tail",
            ("cookie-field-head", "cookie-field-tail"),
            "[REDACTED]",
            id="unquoted-cookie-to-field-end",
        ),
        pytest.param(
            "result",
            "prefix password:password-head-alpha password-tail-bravo password-tail-charlie| detail=safe\nnext line stays",
            ("password-head-alpha", "password-tail-bravo", "password-tail-charlie"),
            "prefix [REDACTED]| detail=safe\nnext line stays",
            id="unquoted-password-to-pipe",
        ),
        pytest.param(
            "verification",
            "prefix api_key=api-head-alpha api-tail-bravo api-tail-charlie, detail=safe\nnext line stays",
            ("api-head-alpha", "api-tail-bravo", "api-tail-charlie"),
            "prefix [REDACTED], detail=safe\nnext line stays",
            id="unquoted-api-key-to-comma",
        ),
        pytest.param(
            "verification",
            "prefix token=token-head-alpha token-tail-bravo token-tail-charlie\nnext line stays",
            ("token-head-alpha", "token-tail-bravo", "token-tail-charlie"),
            "prefix [REDACTED]\nnext line stays",
            id="unquoted-token-to-newline",
        ),
        pytest.param(
            "result",
            "prefix auth=auth-head-alpha auth-tail-bravo auth-tail-charlie&detail=safe\nnext line stays",
            ("auth-head-alpha", "auth-tail-bravo", "auth-tail-charlie"),
            "prefix [REDACTED]&detail=safe\nnext line stays",
            id="unquoted-auth-to-ampersand",
        ),
        pytest.param(
            "verification",
            "prefix Authorization=authorization-head-alpha authorization-tail-bravo authorization-tail-charlie; detail=safe\nnext line stays",
            (
                "authorization-head-alpha",
                "authorization-tail-bravo",
                "authorization-tail-charlie",
            ),
            "prefix [REDACTED]; detail=safe\nnext line stays",
            id="unquoted-authorization-to-semicolon",
        ),
        pytest.param(
            "result",
            "prefix credential=credential-head-alpha credential-tail-bravo credential-tail-charlie; detail=safe\nnext line stays",
            ("credential-head-alpha", "credential-tail-bravo", "credential-tail-charlie"),
            "prefix [REDACTED]; detail=safe\nnext line stays",
            id="unquoted-credential-to-semicolon",
        ),
        pytest.param(
            "result",
            "prefix secret=secret-head-alpha secret-tail-bravo secret-tail-charlie; detail=safe\nnext line stays",
            ("secret-head-alpha", "secret-tail-bravo", "secret-tail-charlie"),
            "prefix [REDACTED]; detail=safe\nnext line stays",
            id="unquoted-secret-to-semicolon",
        ),
        pytest.param(
            "verification",
            "prefix private_key=private-head-alpha private-tail-bravo private-tail-charlie] detail=safe\nnext line stays",
            ("private-head-alpha", "private-tail-bravo", "private-tail-charlie"),
            "prefix [REDACTED]] detail=safe\nnext line stays",
            id="unquoted-private-key-to-bracket",
        ),
        pytest.param(
            "result",
            "prefix access-key=access-head-alpha access-tail-bravo access-tail-charlie} detail=safe\nnext line stays",
            ("access-head-alpha", "access-tail-bravo", "access-tail-charlie"),
            "prefix [REDACTED]} detail=safe\nnext line stays",
            id="unquoted-access-key-to-brace",
        ),
    ],
)
def test_terminal_multiword_secrets_leave_no_tail_in_any_sqlite_text(
    tmp_path: Path,
    field: str,
    sensitive_text: str,
    secret_fragments: tuple[str, ...],
    expected_safe_text: str,
) -> None:
    runtime, service = _service(tmp_path)
    arguments = {
        "verification": "safe verification",
        "result": "safe result",
        field: sensitive_text,
    }
    try:
        terminal = service.record_terminal(
            channel="openclaw",
            scope=BASE_SCOPE,
            end_kind="task_end",
            session_id="session-multiword-secret",
            event_id=f"turn-{field}",
            task_type="security.redaction",
            success=True,
            tool_receipts=[],
            **arguments,
        )
        persisted = _all_sqlite_text(runtime)
    finally:
        runtime.close()

    assert terminal["event"][field] == expected_safe_text
    for secret_fragment in secret_fragments:
        assert secret_fragment not in persisted
