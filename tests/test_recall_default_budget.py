"""Default recall path must inject a ≤3s deadline without Lightweight env."""
from __future__ import annotations

from time import perf_counter
from unittest.mock import MagicMock

from eimemory.api.memory import MemoryAPI
from eimemory.retrieval.contracts import CandidateRequest


def _patch_routes(monkeypatch):
    monkeypatch.setattr("eimemory.llm.command_client.bind_verifier_route", lambda *a, **k: object())
    monkeypatch.setattr("eimemory.llm.command_client.reset_verifier_route", lambda *a, **k: None)
    monkeypatch.setattr("eimemory.retrieval.caller_assistance.route_for_channel", lambda *a, **k: None)


def test_default_assistance_deadline_matches_the_three_second_budget(monkeypatch):
    monkeypatch.delenv("EIMEMORY_RECALL_BUDGET_SECONDS", raising=False)
    from eimemory.retrieval.engine import admission_deadlines

    deadline, assistance = admission_deadlines(100.0, started=10.0)
    assert (deadline, assistance) == (13.0, 13.0)


def test_configured_recall_budget_extends_the_assistance_deadline(monkeypatch):
    monkeypatch.setenv("EIMEMORY_RECALL_BUDGET_SECONDS", "8")
    from eimemory.retrieval.engine import admission_deadlines

    deadline, assistance = admission_deadlines(0.0, started=10.0)
    assert (deadline, assistance) == (18.0, 18.0)
    earlier, earlier_assistance = admission_deadlines(12.0, started=10.0)
    assert (earlier, earlier_assistance) == (12.0, 12.0)


def test_memory_api_recall_setdefault_deadline_within_3s(monkeypatch):
    engine = MagicMock()
    captured: dict = {}

    def fake_recall(request: CandidateRequest):
        captured["deadline"] = request.task_context_dict().get("_recall_deadline_monotonic")
        return MagicMock()

    engine.recall.side_effect = fake_recall
    api = MemoryAPI.__new__(MemoryAPI)
    api.store = MagicMock()
    api.recall_engine = engine
    _patch_routes(monkeypatch)

    before = perf_counter()
    api.recall(
        query="hello",
        scope={"org_id": "o", "agent_id": "a", "workspace_id": "w", "user_id": "u"},
        limit=3,
    )
    after = perf_counter()
    deadline = float(captured["deadline"])
    assert before < deadline <= after + 3.0 + 0.05
    assert deadline - before <= 3.0 + 0.05


def test_memory_api_recall_never_extends_tighter_caller_deadline(monkeypatch):
    engine = MagicMock()
    captured: dict = {}

    def fake_recall(request: CandidateRequest):
        captured["deadline"] = request.task_context_dict().get("_recall_deadline_monotonic")
        return MagicMock()

    engine.recall.side_effect = fake_recall
    api = MemoryAPI.__new__(MemoryAPI)
    api.store = MagicMock()
    api.recall_engine = engine
    _patch_routes(monkeypatch)

    now = perf_counter()
    tight = now + 0.8
    api.recall(
        query="hello",
        scope={"org_id": "o", "agent_id": "a", "workspace_id": "w", "user_id": "u"},
        task_context={"_recall_deadline_monotonic": tight},
        limit=3,
    )
    assert abs(float(captured["deadline"]) - tight) < 0.05


def test_memory_api_recall_caps_looser_than_3s_deadline(monkeypatch):
    engine = MagicMock()
    captured: dict = {}

    def fake_recall(request: CandidateRequest):
        captured["deadline"] = request.task_context_dict().get("_recall_deadline_monotonic")
        return MagicMock()

    engine.recall.side_effect = fake_recall
    api = MemoryAPI.__new__(MemoryAPI)
    api.store = MagicMock()
    api.recall_engine = engine
    _patch_routes(monkeypatch)

    before = perf_counter()
    api.recall(
        query="hello",
        scope={"org_id": "o", "agent_id": "a", "workspace_id": "w", "user_id": "u"},
        task_context={"_recall_deadline_monotonic": before + 30.0},
        limit=3,
    )
    deadline = float(captured["deadline"])
    assert deadline - before <= 3.0 + 0.05
