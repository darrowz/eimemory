"""B1: RPC/OpenClaw outcome paths must feed reward→RL like CLI."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from eimemory.api.runtime import Runtime


def test_record_outcome_trace_sinks_post_experience_hook(monkeypatch):
    calls: list[tuple] = []

    def fake_trace(runtime, payload, **kwargs):
        return {"ok": True, "record_id": "trace-1", "kind": "outcome_trace"}

    def fake_hook(runtime, result, scope):
        calls.append((result.get("record_id"), scope))
        return {"eval": {"ok": True}, "rl": {"ok": True}, "memory": {"record_id": "m1"}}

    monkeypatch.setattr("eimemory.experience.record_outcome_trace", fake_trace)
    monkeypatch.setattr("eimemory.governance.closed_loop.post_experience_hook", fake_hook)

    runtime = Runtime.__new__(Runtime)
    runtime.capability_catalog = None
    out = runtime.record_outcome_trace({"outcome": "good"}, scope={"agent_id": "a"})
    assert out["closed_loop"]["rl"]["ok"] is True
    assert calls == [("trace-1", {"agent_id": "a"})]


def test_record_outcome_sinks_lightweight_learning_hook(monkeypatch):
    calls: list[dict] = []

    runtime = Runtime.__new__(Runtime)
    runtime.store = MagicMock()
    runtime.store.record_outcome.return_value = {
        "id": "outcome-1",
        "event_id": "evt-1",
        "outcome": "good",
    }

    monkeypatch.setattr(
        "eimemory.governance.promotion_watch.record_outcome_observations",
        lambda *a, **k: [],
    )

    def fake_light(runtime, result, scope):
        calls.append(dict(result))
        return {"mode": "lightweight", "rl": {"ok": True}, "memory": {"record_id": "m2"}}

    monkeypatch.setattr(
        "eimemory.governance.closed_loop.lightweight_outcome_learning_hook",
        fake_light,
    )
    out = runtime.record_outcome("evt-1", {"outcome": "good"}, scope={"agent_id": "a"})
    assert out["closed_loop"]["mode"] == "lightweight"
    assert calls[0]["record_id"] == "outcome-1"


def _terminal_runtime(monkeypatch, *, rehearsal: bool):
    calls: list[dict] = []
    runtime = Runtime.__new__(Runtime)
    runtime.store = MagicMock()
    runtime.store.record_terminal_bundle.return_value = {
        "event": {"id": "evt-9"},
        "outcome": {"id": "outcome-9", "event_id": "evt-9", "outcome": "good"},
        "outcome_trace": {"ok": True},
    }
    monkeypatch.setattr(
        "eimemory.governance.promotion_watch.record_outcome_observations",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "eimemory.governance.closed_loop.lightweight_outcome_learning_hook",
        lambda runtime, result, scope: calls.append(dict(result)) or {"mode": "lightweight", "rl": {"ok": True}},
    )
    trace_record = SimpleNamespace(content={"payload": {"outcome": {"status": "good", "rehearsal": rehearsal}}})
    out = runtime.record_terminal_bundle(trace_record=trace_record, scope={"agent_id": "codex"})
    return out, calls


def test_terminal_bundle_feeds_reward_like_record_outcome(monkeypatch):
    out, calls = _terminal_runtime(monkeypatch, rehearsal=False)
    assert out["outcome"]["closed_loop"]["rl"]["ok"] is True
    assert calls[0]["record_id"] == "outcome-9"


def test_terminal_bundle_rehearsal_never_feeds_reward(monkeypatch):
    out, calls = _terminal_runtime(monkeypatch, rehearsal=True)
    assert out["outcome"]["closed_loop"]["skipped"] == "rehearsal"
    assert calls == []
