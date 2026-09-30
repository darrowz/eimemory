"""Real ``hermes.task_end`` producer: emitted only when Hermes completes a turn
that carries a passed host-attested receipt binding."""

from __future__ import annotations

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.timeout_seconds = 5.0

    def call_or_bypass(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "adapter.record_terminal":
            return {"ok": True, "result": {"ok": True}}
        return {"ok": True, "result": {"ok": True, "context": ""}}


class FakeHandoff:
    def __init__(self) -> None:
        self.ids: dict[tuple[str, str], list[str]] = {}
        self.cleared: list[tuple[str, str, list[str]]] = []

    def list_ids(self, *, channel, scope, session_id, run_id):
        del channel, scope
        return list(self.ids.get((session_id, run_id), []))

    def clear_exact(self, *, channel, scope, session_id, run_id, receipt_ids):
        del channel, scope
        self.cleared.append((session_id, run_id, list(receipt_ids)))
        self.ids.pop((session_id, run_id), None)


def _provider(client: RecordingClient, handoff: FakeHandoff | None = None) -> HermesMemoryProviderCore:
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize("session-a", agent_workspace="embodied", agent_context="primary")
    provider._receipt_handoff = handoff if handoff is not None else FakeHandoff()
    return provider


def _complete(provider: HermesMemoryProviderCore, turn_id: str) -> None:
    provider.on_post_llm_call(
        user_message="run the tests",
        assistant_message="tests pass",
        session_id="session-a",
        turn_id=turn_id,
    )
    worker = provider._auto_task_end_thread
    if worker is not None:
        worker.join(timeout=5)


def _terminals(client: RecordingClient) -> list[dict]:
    return [params for method, params in client.calls if method == "adapter.record_terminal"]


def test_completed_turn_with_passed_host_receipt_emits_one_task_end() -> None:
    client = RecordingClient()
    handoff = FakeHandoff()
    handoff.ids[("session-a", "turn-7")] = ["rcpt_a"]
    provider = _provider(client, handoff)
    assert provider.bind_verified_host_turn(session_id="session-a", turn_id="turn-7") is True

    _complete(provider, "turn-7")

    terminals = _terminals(client)
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal["end_kind"] == "task_end"
    assert terminal["session_id"] == "session-a"
    assert terminal["event_id"] == "turn-7"
    # The producer never claims success or a task type; the runtime derives
    # both from the exact persisted receipts.
    assert terminal["success"] is None
    assert terminal["task_type"] == "research.unverified"
    assert terminal["rehearsal"] is False
    assert terminal["receipt_ids"] == ["rcpt_a"]
    assert handoff.cleared == [("session-a", "turn-7", ["rcpt_a"])]
    assert ("session-a", "turn-7") not in provider._verified_host_turns

    # The binding is consumed: completing again produces nothing more.
    _complete(provider, "turn-7")
    assert len(_terminals(client)) == 1


def test_observed_only_turn_and_other_turns_emit_nothing() -> None:
    client = RecordingClient()
    provider = _provider(client)
    provider.bind_observed_host_turn(session_id="session-a", turn_id="turn-1")
    _complete(provider, "turn-1")
    assert _terminals(client) == []

    provider.bind_verified_host_turn(session_id="session-a", turn_id="turn-2")
    _complete(provider, "turn-3")
    _complete(provider, "")
    assert _terminals(client) == []


def test_task_end_producer_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_HERMES_AUTO_TASK_END", "0")
    client = RecordingClient()
    provider = _provider(client)
    provider.bind_verified_host_turn(session_id="session-a", turn_id="turn-7")
    _complete(provider, "turn-7")
    assert _terminals(client) == []
    # The model-requested closer still works while the producer is off.
    assert list(provider._verified_host_turns) == [("session-a", "turn-7")]


def test_verified_binding_without_handed_off_receipts_emits_nothing() -> None:
    client = RecordingClient()
    provider = _provider(client, FakeHandoff())
    provider.bind_verified_host_turn(session_id="session-a", turn_id="turn-7")
    _complete(provider, "turn-7")
    assert _terminals(client) == []
    provider._receipt_handoff = None
    _complete(provider, "turn-7")
    assert _terminals(client) == []
