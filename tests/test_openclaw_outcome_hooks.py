from __future__ import annotations

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle


def _build_bundle(*, task_context: dict, query: str = "open dashboard") -> RecallBundle:
    return RecallBundle(
        items=[],
        rules=[],
        reflections=[],
        confidence=0.7,
        next_action_hint="",
        explanation={
            "query": query,
            "task_context": dict(task_context),
            "selected_count": 1,
            "active_policy": {},
            "rule_count": 0,
            "unknown_record_id": "",
            "graph_expanded": 0,
            "retrieval_mode": "hybrid",
            "policy_suggestion_ids": ["policy-1"],
            "policy_sources": ["intent_pattern"],
            "matched_event_type": "browser_task",
            "selected_records": [
                {
                    "record_id": "rec-1",
                    "kind": "memory",
                    "title": "Dashboard policy",
                    "source": "test",
                    "projection_type": "",
                    "source_record_id": "",
                }
            ],
        },
    )


def test_openclaw_before_prompt_build_returns_trace_context_and_policy_attribution(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    captured: dict[str, object] = {}

    def fake_recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        captured["task_context"] = dict(task_context)
        return _build_bundle(task_context=task_context, query=query)

    monkeypatch.setattr(runtime.memory, "recall", fake_recall)
    monkeypatch.setattr(
        runtime,
        "search_policy",
        lambda query, *, scope, context, limit: {
            "ok": True,
            "matched_event_type": "browser_task",
            "policy_suggestions": [{"id": "policy-1", "source": "intent_pattern"}],
        },
    )

    result = hooks.before_prompt_build(
        {
            "session_id": "sess-trace",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "open dashboard",
            "trace_id": "trace-123",
            "idempotency_key": "idem-123",
            "started_at": "2026-06-01T10:00:00Z",
            "task_context": {"task_type": "browser_task"},
        }
    )

    trace_context = result["trace_context"]
    assert trace_context == {
        "trace_id": "trace-123",
        "idempotency_key": "idem-123",
        "task_type": "browser_task",
        "started_at": "2026-06-01T10:00:00Z",
        "query": "open dashboard",
    }
    assert captured["task_context"]["trace_context"] == trace_context
    assert result["task_context"]["policy_attribution"]["policy_suggestion_ids"] == ["policy-1"]
    assert result["task_context"]["policy_attribution"]["policy_sources"] == ["intent_pattern"]
    assert result["task_context"]["policy_attribution"]["matched_event_type"] == "browser_task"
    assert result["task_context"]["policy_attribution"]["selected_records"][0]["record_id"] == "rec-1"
    assert result["memory_bundle"]["explanation"]["policy_suggestion_ids"] == ["policy-1"]


def test_openclaw_agent_end_records_success_outcome_trace(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[tuple[dict, dict]] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append((payload, scope))
        return {"id": "trace-record-1", "payload": payload}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-success",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "open dashboard",
            "task_context": {
                "task_type": "browser_task",
                "policy_suggestion_ids": ["policy-1"],
                "policy_sources": ["intent_pattern"],
                "matched_event_type": "browser_task",
                "selected_records": [{"record_id": "rec-1"}],
            },
            "tool_calls": [{"name": "browser.open"}],
            "action_path": ["navigate dashboard"],
            "world_state": {"url": "https://example.test/dashboard"},
            "visual_evidence": {"screenshot_id": "shot-1"},
            "operator_gap": {"missing": "none"},
            "outcome": {"success": True, "verified": True, "feedback": "looks good"},
        }
    )

    assert result["outcome_trace"]["id"] == "trace-record-1"
    payload, scope = traces[0]
    assert scope["user_id"] == "darrow"
    assert payload["trace_id"] == "openclaw:sess-success:browser_task:open dashboard"
    assert payload["idempotency_key"] == "openclaw:outcome:sess-success:browser_task:open dashboard"
    assert payload["input_summary"] == "open dashboard"
    assert payload["selected_tools"] == ["browser.open"]
    assert payload["actions"] == ["navigate dashboard"]
    assert payload["outcome"] == "success"
    assert payload["verifier"] == "verified"
    assert payload["feedback"] == "looks good"
    assert payload["risk"] == ""
    assert payload["policy_attribution"]["policy_suggestion_ids"] == ["policy-1"]
    assert payload["policy_attribution"]["selected_records"] == [{"record_id": "rec-1"}]
    assert payload["world_state"] == {"url": "https://example.test/dashboard"}
    assert payload["visual_evidence"] == {"screenshot_id": "shot-1"}
    assert payload["operator_gap"] == {"missing": "none"}


def test_openclaw_agent_end_persists_outcome_trace_through_runtime(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    result = hooks.on_agent_end(
        {
            "session_id": "sess-real-trace",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "open dashboard",
            "task_context": {"task_type": "browser_task"},
            "tool_calls": [{"name": "browser.open"}],
            "outcome": {"success": True, "verified": True, "verification": "page loaded"},
        }
    )

    assert result["outcome_trace"]["ok"] is True
    record = runtime.store.get_by_id(result["outcome_trace"]["record_id"], scope=scope)
    assert record is not None
    assert record.meta["report_type"] == "outcome_trace"
    assert record.meta["trace_id"] == "openclaw:sess-real-trace:browser_task:open dashboard"
    assert record.content["payload"]["trace_id"] == "openclaw:sess-real-trace:browser_task:open dashboard"


def test_openclaw_trace_context_distinguishes_repeated_attempts_with_started_at(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    base_event = {
        "session_id": "sess-retry",
        "agent_id": "main",
        "workspace_id": "repo-x",
        "user_id": "darrow",
        "query": "open dashboard",
        "task_context": {"task_type": "browser_task"},
        "outcome": {"success": False, "notes": "navigation failed"},
    }
    first = hooks.on_agent_end({**base_event, "started_at": "2026-06-01T10:00:00Z"})
    second = hooks.on_agent_end({**base_event, "started_at": "2026-06-01T10:01:00Z"})

    assert first["outcome_trace"]["ok"] is True
    assert second["outcome_trace"]["ok"] is True
    assert first["outcome_trace"]["record_id"] != second["outcome_trace"]["record_id"]
    traces = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
        if record.meta.get("report_type") == "outcome_trace"
    ]
    assert len(traces) == 2


def test_openclaw_trace_context_preserves_nested_fields_and_attempt_ids(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    scope = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    base_event = {
        "session_id": "sess-nested-retry",
        "agent_id": "main",
        "workspace_id": "repo-x",
        "user_id": "darrow",
        "query": "open dashboard",
        "task_context": {"task_type": "browser_task"},
        "outcome": {"success": False, "notes": "navigation failed"},
    }
    first = hooks.on_agent_end(
        {
            **base_event,
            "task_context": {
                "task_type": "browser_task",
                "trace_context": {
                    "task_id": "attempt-1",
                    "span_id": "span-1",
                    "parent_trace_id": "parent-trace",
                },
            },
        }
    )
    second = hooks.on_agent_end(
        {
            **base_event,
            "task_context": {
                "task_type": "browser_task",
                "trace_context": {
                    "task_id": "attempt-2",
                    "span_id": "span-2",
                    "parent_trace_id": "parent-trace",
                },
            },
        }
    )

    assert first["outcome_trace"]["record_id"] != second["outcome_trace"]["record_id"]
    traces = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
        if record.meta.get("report_type") == "outcome_trace"
    ]
    trace_contexts = [record.content["payload"]["trace_context"] for record in traces]
    assert {item["task_id"] for item in trace_contexts} == {"attempt-1", "attempt-2"}
    assert {item["span_id"] for item in trace_contexts} == {"span-1", "span-2"}
    assert all(item["parent_trace_id"] == "parent-trace" for item in trace_contexts)


def test_openclaw_agent_end_records_bad_outcome_trace_for_failure(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[dict] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append(payload)
        return {"id": "trace-record-bad"}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    hooks.on_agent_end(
        {
            "session_id": "sess-failure",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [{"content": "open dashboard"}],
            "outcome": {"success": False, "notes": "navigation failed"},
            "risk": {"severity": "medium"},
        }
    )

    payload = traces[0]
    assert payload["outcome"] == "bad"
    assert payload["feedback"] == "navigation failed"
    assert payload["risk"] == {"severity": "medium"}


def test_openclaw_record_outcome_trace_exception_degrades_without_raising(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        raise RuntimeError("trace store offline")

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-trace-error",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "user_messages": [{"content": "open dashboard"}],
            "outcome": {"success": True, "verified": True},
        }
    )

    assert result["outcome_trace_error"] == "trace store offline"
