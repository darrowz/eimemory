from __future__ import annotations

from pathlib import Path

import pytest

from eimemory.adapters.runtime.channel import base_scope_from_channel, resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def _service(tmp_path: Path) -> AgentRuntimeMemoryService:
    return AgentRuntimeMemoryService(Runtime.create(root=tmp_path))


def test_channel_scope_is_deterministic_and_openclaw_compatible() -> None:
    assert resolve_channel_scope("openclaw", BASE_SCOPE) == BASE_SCOPE
    assert resolve_channel_scope("codex", BASE_SCOPE)["workspace_id"] == "embodied::channel::codex"
    assert resolve_channel_scope("hermes", BASE_SCOPE)["workspace_id"] == "embodied::channel::hermes"
    assert (
        resolve_channel_scope(
            "codex",
            {**BASE_SCOPE, "workspace_id": "embodied::channel::codex"},
        )["workspace_id"]
        == "embodied::channel::codex"
    )


@pytest.mark.parametrize("channel", ["codex", "hermes"])
def test_empty_workspace_channel_scope_round_trips_without_implicit_default(channel: str) -> None:
    base_scope = {**BASE_SCOPE, "workspace_id": ""}

    channel_scope = resolve_channel_scope(channel, base_scope)

    assert channel_scope["workspace_id"] == f"::channel::{channel}"
    assert base_scope_from_channel(channel, channel_scope) == base_scope


def test_unknown_runtime_channel_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported runtime channel"):
        resolve_channel_scope("unknown-host", BASE_SCOPE)


def test_codex_and_hermes_memories_are_independent_authoritative_records(tmp_path: Path) -> None:
    service = _service(tmp_path)
    codex_text = "Always keep Codex deployment reports concise and include the verified release identity."
    hermes_text = "Always keep Hermes research summaries detailed and include the supporting evidence."

    codex = service.remember(
        channel="codex",
        scope=BASE_SCOPE,
        text=codex_text,
        memory_type="preference",
        event_id="codex-memory-1",
    )
    hermes = service.remember(
        channel="hermes",
        scope=BASE_SCOPE,
        text=hermes_text,
        memory_type="preference",
        event_id="hermes-memory-1",
    )

    assert codex["ok"] is True
    assert hermes["ok"] is True
    assert codex["record"]["status"] == "active"
    assert hermes["record"]["status"] == "active"
    assert codex["record"]["scope"]["workspace_id"] == "embodied::channel::codex"
    assert hermes["record"]["scope"]["workspace_id"] == "embodied::channel::hermes"
    assert codex["record"]["meta"]["runtime_channel"] == "codex"
    assert hermes["record"]["meta"]["runtime_channel"] == "hermes"
    assert codex["record"]["meta"]["authority_mode"] == "per_channel"
    assert codex["record"]["meta"]["authoritative"] is True
    assert hermes["record"]["meta"]["authoritative"] is True

    codex_recall = service.prefetch(
        channel="codex",
        scope=BASE_SCOPE,
        query="deployment reports concise release identity",
        task_type="code.release",
    )
    hermes_recall = service.prefetch(
        channel="hermes",
        scope=BASE_SCOPE,
        query="deployment reports concise release identity",
        task_type="research.summary",
    )

    assert [item["record_id"] for item in codex_recall["bundle"]["items"]] == [
        codex["record"]["record_id"]
    ]
    assert hermes_recall["bundle"]["items"] == []
    assert codex_text in codex_recall["context"]


def test_prefetch_invalid_limit_falls_back_to_bounded_default(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    observed: dict[str, int] = {}
    original_recall = service.runtime.memory.recall

    def capture_limit(**kwargs):
        observed["limit"] = kwargs["limit"]
        return original_recall(**kwargs)

    monkeypatch.setattr(service.runtime.memory, "recall", capture_limit)

    result = service.prefetch(
        channel="codex",
        scope=BASE_SCOPE,
        query="bounded default",
        limit=None,  # type: ignore[arg-type] - exercise untrusted RPC input
    )

    assert result["ok"] is True
    assert result["channel"] == "codex"
    assert observed["limit"] == 8


def test_explicit_memory_write_is_idempotent_per_channel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    params = {
        "channel": "codex",
        "scope": BASE_SCOPE,
        "text": "Remember that Codex verifies release health before declaring deployment complete.",
        "memory_type": "durable_fact",
        "event_id": "turn-42-memory",
    }

    first = service.remember(**params)
    second = service.remember(**params)

    assert first["record"]["record_id"] == second["record"]["record_id"]
    assert first["idempotent"] is False
    assert second["idempotent"] is True


def test_sync_turn_truncates_payload_and_reuses_turn_id(tmp_path: Path) -> None:
    service = AgentRuntimeMemoryService(Runtime.create(root=tmp_path), max_turn_chars=160)
    user_text = "Remember this Codex turn preference. " + ("u" * 300)
    assistant_text = "The preference was stored and verified. " + ("a" * 300)

    first = service.sync_turn(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        turn_id="turn-1",
        user_text=user_text,
        assistant_text=assistant_text,
    )
    second = service.sync_turn(
        channel="codex",
        scope=BASE_SCOPE,
        session_id="session-1",
        turn_id="turn-1",
        user_text=user_text,
        assistant_text=assistant_text,
    )

    assert len(first["record"]["content"]["text"]) <= 160
    assert second["idempotent"] is True
    assert first["record"]["record_id"] == second["record"]["record_id"]


def test_unverified_success_records_terminal_evidence_without_verified_pass(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.record_terminal(
        channel="codex",
        scope=BASE_SCOPE,
        end_kind="stop",
        session_id="codex-session-1",
        event_id="codex-turn-1",
        task_type="code.fix",
        success=True,
        verification="",
        result="changes written",
    )

    assert result["event"]["source"] == "codex.stop"
    assert result["outcome"]["outcome"] == "verification_missing"
    assert result["outcome_trace"]["ok"] is True
    trace = service.runtime.store.get_by_id(
        result["outcome_trace"]["record_id"],
        scope=ScopeRef.from_dict(resolve_channel_scope("codex", BASE_SCOPE)),
    )
    assert trace is not None
    assert trace.content["payload"]["verifier"]["passed"] is False
    assert trace.content["payload"]["outcome"]["success"] is True


def test_session_end_is_lifecycle_only_and_does_not_create_outcome_trace(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.record_terminal(
        channel="hermes",
        scope=BASE_SCOPE,
        end_kind="session_end",
        session_id="hermes-session-1",
        event_id="hermes-session-end-1",
        task_type="research.summary",
        success=True,
        verification="pytest:passed",
        result="session closed",
    )

    assert result["event"]["source"] == "hermes.session_end"
    assert result["event"]["evidence_class"] == "lifecycle_event"
    assert result["outcome_trace"] is None
