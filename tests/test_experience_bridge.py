from eimemory.api.runtime import Runtime
import eimemory.experience
from eimemory.experience import record_experience_item, record_skill_trace


def _trace_payload() -> dict:
    return {
        "trace_id": "trace-123",
        "task_type": "repo.edit",
        "input_summary": "Add bridge API",
        "selected_skills": [{"skill_id": "skill.tdd"}, "skill.verify"],
        "actions": [{"type": "test", "name": "pytest"}],
        "outcome": "success",
        "feedback": {"review": "useful"},
        "latency_ms": 42,
    }


def test_record_skill_trace_writes_reflection_with_meta_and_content(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = record_skill_trace(runtime, _trace_payload(), scope={"agent_id": "eibrain", "workspace_id": "repo"})

    assert result["ok"] is True
    record = runtime.store.get_by_id(result["record_id"], scope={"agent_id": "eibrain", "workspace_id": "repo"})
    assert record is not None
    assert record.kind == "reflection"
    assert record.source == "eimemory.experience.skill_trace"
    assert record.content == _trace_payload()
    assert record.meta["report_type"] == "skill_trace"
    assert record.meta["selected_skill_ids"] == ["skill.tdd", "skill.verify"]
    assert record.meta["outcome"] == "success"
    assert record.meta["task_type"] == "repo.edit"


def test_record_experience_item_writes_reflection_with_experience_meta(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    payload = {
        "experience_id": "exp-1",
        "experience_kind": "skill_pattern",
        "summary": "TDD caught the missing bridge module",
        "skill_ids": ["skill.tdd", "skill.verify"],
        "confidence": 0.84,
        "outcome_delta": {"quality": "+regression coverage"},
    }

    result = record_experience_item(runtime, payload, scope={"tenant_id": "tenant-a", "user_id": "alice"})

    assert result["ok"] is True
    record = runtime.store.get_by_id(result["record_id"], scope={"tenant_id": "tenant-a", "user_id": "alice"})
    assert record is not None
    assert record.kind == "reflection"
    assert record.source == "eimemory.experience.item"
    assert record.content == payload
    assert record.meta["experience_kind"] == "skill_pattern"
    assert record.meta["skill_ids"] == ["skill.tdd", "skill.verify"]
    assert record.meta["confidence"] == 0.84
    assert record.meta["outcome_delta"] == {"quality": "+regression coverage"}


def test_runtime_exposes_experience_bridge_methods_and_keeps_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {
        "tenant_id": "tenant-a",
        "agent_id": "openclaw",
        "workspace_id": "robot",
        "user_id": "operator",
    }

    result = runtime.record_skill_trace(_trace_payload(), scope=scope)
    item_result = runtime.record_experience_item(
        {"experience_kind": "lesson", "skill_ids": ["skill.verify"], "confidence": 0.9, "outcome_delta": "faster"},
        scope=scope,
    )

    assert result["ok"] is True
    assert item_result["ok"] is True
    record = runtime.store.get_by_id(result["record_id"], scope=scope)
    item_record = runtime.store.get_by_id(item_result["record_id"], scope=scope)
    assert record is not None
    assert item_record is not None
    assert record.scope.tenant_id == "tenant-a"
    assert record.scope.agent_id == "openclaw"
    assert record.scope.workspace_id == "robot"
    assert record.scope.user_id == "operator"
    assert item_record.source == "eimemory.experience.item"


def test_runtime_exposes_record_outcome_trace(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[tuple[object, dict, dict | None]] = []

    def _record_outcome_trace(runtime_arg, payload, *, scope=None):
        calls.append((runtime_arg, payload, scope))
        return {"ok": True, "record_id": "outcome-trace-1"}

    monkeypatch.setattr(eimemory.experience, "record_outcome_trace", _record_outcome_trace, raising=False)

    result = runtime.record_outcome_trace({"trace_id": "trace-1"}, scope={"agent_id": "eibrain"})

    assert result == {"ok": True, "record_id": "outcome-trace-1"}
    assert calls == [(runtime, {"trace_id": "trace-1"}, {"agent_id": "eibrain"})]


def test_invalid_payload_returns_error_and_does_not_write(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = record_skill_trace(runtime, {"trace_id": "missing-required-fields"}, scope={"agent_id": "eibrain"})

    assert result["ok"] is False
    assert result["error"]
    assert runtime.store.list_records(kinds=["reflection"], scope={"agent_id": "eibrain"}, limit=10) == []
