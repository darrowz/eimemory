from __future__ import annotations

from types import SimpleNamespace

from eimemory.cli.main import COMMAND_REGISTRY, dispatch, register
from eimemory.governance.closed_loop import autonomy_cycle, post_experience_hook


class _Memory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def ingest(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            record_id=f"memory-{len(self.calls)}",
            to_dict=lambda: {"record_id": f"memory-{len(self.calls)}", "kind": "memory", "title": kwargs.get("title")},
        )


class _Store:
    def __init__(self, record=None) -> None:
        self.record = record

    def get_by_id(self, record_id, scope=None):
        if self.record and self.record.record_id == record_id:
            return self.record
        return None


class _Runtime:
    def __init__(self, record=None) -> None:
        self.memory = _Memory()
        self.store = _Store(record)
        self.learning_calls: list[dict] = []
        self.autonomy_calls: list[dict] = []

    def generate_learning_thoughts(self, **kwargs):
        self.learning_calls.append(kwargs)
        return {"ok": True, "thoughts": [{"id": "thought-1", "capability": "memory.recall"}]}

    def run_autonomy_cycle(self, **kwargs):
        self.autonomy_calls.append(kwargs)
        return {"ok": True, "cycle_id": "cycle-1", "promoted_count": 1}


def test_command_dispatcher_runs_registered_handler_and_reports_unknown() -> None:
    command = "__unit_dispatch__"

    @register(command)
    def _unit(parsed, runtime, scope):
        return {"ok": True, "command": parsed.command, "scope": scope}

    try:
        parsed = SimpleNamespace(command=command)
        assert dispatch(command, parsed, object(), {"agent_id": "eibrain"}) == {
            "ok": True,
            "command": command,
            "scope": {"agent_id": "eibrain"},
        }
        assert dispatch("__missing__", parsed, object(), {}) == {"ok": False, "error": "unknown_command"}
    finally:
        COMMAND_REGISTRY.pop(command, None)


def test_post_experience_hook_writes_feedback_memory_and_generates_learning() -> None:
    record = SimpleNamespace(
        record_id="trace-1",
        meta={
            "report_type": "outcome_trace",
            "primary_label": "missing_tool_call",
            "signals": ["operator_gap"],
            "outcome_status": "bad",
        },
        content={"diagnosis": {"confidence": 0.81}},
    )
    runtime = _Runtime(record)
    result = post_experience_hook(runtime, {"ok": True, "record_id": "trace-1"}, scope={"agent_id": "eibrain"})

    assert result["eval"]["primary_label"] == "missing_tool_call"
    assert result["eval"]["ok"] is False
    assert runtime.memory.calls[0]["title"] == "auto-feedback"
    assert runtime.memory.calls[0]["memory_type"] == "reflection"
    assert runtime.memory.calls[0]["source"] == "loop"
    assert runtime.memory.calls[0]["force_capture"] is True
    assert runtime.learning_calls == [{"scope": {"agent_id": "eibrain"}, "persist": True, "max_items": 3}]
    assert result["memory"]["record_id"] == "memory-1"
    assert result["learning"]["ok"] is True


def test_autonomy_cycle_writes_feedback_memory_after_cycle() -> None:
    runtime = _Runtime()
    result = autonomy_cycle(runtime, {"agent_id": "eibrain"}, apply=True, dry_run=False, max_goals=2)

    assert runtime.autonomy_calls == [{"scope": {"agent_id": "eibrain"}, "apply": True, "dry_run": False, "max_goals": 2}]
    assert result["cycle"]["cycle_id"] == "cycle-1"
    assert result["feedback"]["ok"] is True
    assert runtime.memory.calls[0]["title"] == "autonomy-loop"
    assert runtime.memory.calls[0]["memory_type"] == "autonomy_feedback"
    assert runtime.memory.calls[0]["source"] == "system"
    assert runtime.memory.calls[0]["force_capture"] is True
