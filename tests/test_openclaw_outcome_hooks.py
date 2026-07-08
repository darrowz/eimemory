from __future__ import annotations

import pytest

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.ops import openclaw_loop as loop


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


def _record(
    *,
    kind: str,
    title: str,
    summary: str,
    detail: str = "",
    content: dict | None = None,
    meta: dict | None = None,
) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=summary,
        detail=detail,
        content=content,
        meta=meta,
        scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
        source="test.openclaw",
    )


def test_openclaw_before_prompt_build_returns_trace_context_and_policy_attribution(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "loop"))
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
    assert result["task_context"]["openclaw_loop_task_id"]
    assert loop.get_task(result["task_context"]["openclaw_loop_task_id"])["status"] == "running"


def test_openclaw_task_end_closes_loop_task_and_records_lesson_on_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "loop"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    hooks = OpenClawMemoryHooks(runtime)
    task = loop.create_task(
        title="OpenClaw user request",
        objective="complete user request",
        source="openclaw.before_prompt_build",
        dedupe_key="openclaw:sess-failed",
        report_policy="always",
    )
    loop.record_heartbeat(task["task_id"], progress="started")

    result = hooks.on_task_end(
        {
            "session_id": "sess-failed",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "check dashboard",
            "task_context": {"openclaw_loop_task_id": task["task_id"], "task_type": "browser_task"},
            "outcome": {"success": False, "verified": False, "notes": "health check timeout"},
        }
    )

    closed = loop.get_task(task["task_id"])
    assert closed["status"] == "failed"
    assert result["loop_task"]["status"] == "failed"
    assert loop.read_jsonl("verifications.jsonl")[-1]["passed"] is False
    lessons = loop.read_jsonl("lesson_candidates.jsonl")
    assert lessons[-1]["task_id"] == task["task_id"]
    assert "health check timeout" in lessons[-1]["failure_reason"]


def test_openclaw_before_prompt_build_strict_injection_plan_classifies_and_audits_lanes(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    preference = _record(
        kind="memory",
        title="Reply preference",
        summary="User prefers concise replies with evidence.",
        content={"text": "Always answer with concise replies and include evidence.", "memory_type": "preference"},
        meta={
            "memory_type": "preference",
            "quality": {"confidence": 0.96, "quality_tier": "core", "salience_score": 0.94},
        },
    )
    regular_memory = _record(
        kind="memory",
        title="Recent discussion",
        summary="A recent implementation discussion mentioned dashboard work.",
        content={"text": "Detailed transient discussion log about dashboard implementation.", "memory_type": "conversation"},
        meta={
            "memory_type": "conversation",
            "quality": {"confidence": 0.55, "quality_tier": "candidate", "salience_score": 0.5},
        },
    )
    candidate = _record(
        kind="capability_candidate",
        title="Browser automation candidate",
        summary="Candidate capability should inform policy only.",
    )
    incident = _record(
        kind="incident",
        title="Failed browser run",
        summary="Operational incident should not be injected into prompts.",
    )
    rule = _record(
        kind="rule",
        title="Verify before completion",
        summary="Run verification before claiming completion.",
        content={"execution_policy": ["Run tests before status updates."]},
    )

    def fake_recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        return RecallBundle(
            items=[preference, regular_memory, candidate, incident],
            rules=[rule],
            reflections=[],
            confidence=0.83,
            next_action_hint="",
            explanation={
                "query": query,
                "task_context": dict(task_context),
                "selected_count": 5,
                "active_policy": {},
                "rule_count": 1,
                "unknown_record_id": "",
                "graph_expanded": 0,
                "retrieval_mode": "hybrid",
            },
        )

    monkeypatch.setattr(runtime.memory, "recall", fake_recall)
    monkeypatch.setattr(
        runtime,
        "search_policy",
        lambda query, *, scope, context, limit: {"ok": True, "policy_suggestions": [], "matched_event_type": ""},
    )

    result = hooks.before_prompt_build(
        {
            "session_id": "sess-injection-plan",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "How should I reply about dashboard work?",
            "task_context": {"task_type": "chat.reply"},
        }
    )

    plan = result["task_context"]["injection_plan"]
    lanes = {entry["record_id"]: entry["lane"] for entry in plan["entries"]}
    reasons = {entry["record_id"]: entry.get("withheld_reason", "") for entry in plan["entries"]}
    assert plan["mode"] == "strict"
    assert lanes[preference.record_id] == "full_text"
    assert lanes[regular_memory.record_id] == "summary_only"
    assert lanes[candidate.record_id] == "policy_only"
    assert lanes[rule.record_id] == "policy_only"
    assert lanes[incident.record_id] == "withheld"
    assert reasons[incident.record_id] == "operational_record"
    assert plan["lane_composition"] == {
        "full_text": 1,
        "summary_only": 1,
        "policy_only": 2,
        "withheld": 1,
    }
    assert plan["withheld_reasons"] == {"operational_record": 1}
    assert plan["token_estimate"] >= plan["entries"][0]["token_estimate"] > 0

    telemetry = result["usage_telemetry"]
    assert telemetry["injection_token_estimate"] == plan["token_estimate"]
    assert telemetry["injection_lane_composition"] == plan["lane_composition"]
    assert telemetry["injection_withheld_reasons"] == plan["withheld_reasons"]
    assert result["memory_bundle"]["explanation"]["injection_plan"] == plan

    audits = runtime.store.list_records(
        kinds=["recall_view"],
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        limit=1,
    )
    assert audits[0].content["injection_plan"] == plan
    assert audits[0].content["injection_token_estimate"] == plan["token_estimate"]
    assert audits[0].content["injection_lane_composition"] == plan["lane_composition"]
    assert audits[0].content["injection_withheld_reasons"] == plan["withheld_reasons"]
    assert audits[0].meta["injection_token_estimate"] == plan["token_estimate"]


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


def test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[dict] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append(payload)
        return {"id": "trace-rate-limit"}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-rate-limit",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"task_type": "chat.reply", "bridge_status": "cooldown"},
            "outcome": {
                "success": True,
                "verified": True,
                "notes": "OpenAI rate limit cooldown active; fallback response used.",
            },
        }
    )

    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == "rate_limit_cooldown"
    assert result["outcome"]["source_trust"] == "system_diagnostic"
    assert traces[0]["outcome"] == "bad"
    assert traces[0]["failure_class"] == "rate_limit_cooldown"


@pytest.mark.parametrize(
    ("bridge_status", "failure_class"),
    [
        ("rate_limit", "rate_limit_cooldown"),
        ("rate_limited", "rate_limit_cooldown"),
        ("timeout", "timeout"),
        ("context_overflow", "context_overflow"),
        ("bridge_failure", "bridge_failure"),
        ("model_failure", "model_failure"),
    ],
)
def test_openclaw_agent_end_terminal_bridge_statuses_fail_closed(
    tmp_path, monkeypatch, bridge_status: str, failure_class: str
) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[dict] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append(payload)
        return {"id": f"trace-{bridge_status}"}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": f"sess-{bridge_status}",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"task_type": "chat.reply", "bridge_status": bridge_status},
            "outcome": {
                "success": True,
                "verified": True,
                "notes": "Fallback response used.",
            },
        }
    )

    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == failure_class
    assert result["outcome"]["source_trust"] == "system_diagnostic"
    assert traces[0]["outcome"] == "bad"
    assert traces[0]["failure_class"] == failure_class


def test_openclaw_agent_end_assistant_text_failure_fails_closed(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[dict] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append(payload)
        return {"id": "trace-assistant-text"}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-assistant-text",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"task_type": "chat.reply"},
            "assistant_messages": [
                {"content": "OpenAI rate limit cooldown active; fallback response used."}
            ],
            "outcome": {"success": True, "verified": True},
        }
    )

    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == "rate_limit_cooldown"
    assert result["outcome"]["source_trust"] == "system_diagnostic"
    assert traces[0]["outcome"] == "bad"


def test_openclaw_agent_end_terminal_failure_closes_loop_as_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "loop"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    hooks = OpenClawMemoryHooks(runtime)
    task = loop.create_task(
        title="OpenClaw user request",
        objective="complete user request",
        source="openclaw.before_prompt_build",
        dedupe_key="openclaw:sess-loop-failure",
        report_policy="always",
    )
    loop.record_heartbeat(task["task_id"], progress="started")

    result = hooks.on_agent_end(
        {
            "session_id": "sess-loop-failure",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"openclaw_loop_task_id": task["task_id"], "task_type": "chat.reply"},
            "outcome": {
                "success": True,
                "verified": True,
                "notes": "Model failure: fallback response used.",
            },
        }
    )

    closed = loop.get_task(task["task_id"])
    verification = loop.read_jsonl("verifications.jsonl")[-1]
    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == "model_failure"
    assert closed["status"] == "failed"
    assert result["loop_task"]["status"] == "failed"
    assert verification["passed"] is False
    assert verification["checks"]["failure_class"] == "model_failure"


def test_openclaw_agent_end_terminal_failure_overrides_explicit_source_trust(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-source-trust",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"task_type": "chat.reply", "bridge_status": "timeout"},
            "outcome": {
                "success": True,
                "verified": True,
                "source_trust": "system_verified",
            },
        }
    )

    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == "timeout"
    assert result["outcome"]["source_trust"] == "system_diagnostic"


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
