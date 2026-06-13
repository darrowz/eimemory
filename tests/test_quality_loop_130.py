from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.models.records import RecordEnvelope, ScopeRef


PASSING_EVAL = {
    "verdict": "pass",
    "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8, "evidence": 1.0},
    "gate_bundle": {
        "evidence": [{"tier": "T0", "ref": "evt_1", "summary": "Verified policy outcome"}],
        "rollback": {"available": True, "executable": True},
        "canary": {"passed": True, "blast_radius": "single_scope"},
        "timeout_seconds": 300,
        "audit": {"enabled": True},
        "prompt_shadow_eval": {"passed": True},
        "prompt_injection_check": {"passed": True},
    },
}


def test_recall_quality_report_counts_quality_and_persists(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "quality-loop", "user_id": "darrow"}
    dataset = {
        "name": "quality-loop-smoke",
        "scope": scope,
        "seed": [
            {
                "id": "style",
                "kind": "memory",
                "title": "鸿哥沟通风格",
                "text": "鸿哥沟通风格：极简、直接、讨厌废话。",
                "memory_type": "preference",
            },
            {
                "id": "audit",
                "kind": "reflection",
                "title": "OpenClaw memory injection audit",
                "text": "历史审计记录，不应该进入普通任务召回。",
                "source": "openclaw.before_prompt_build",
                "meta": {"report_type": "recall_audit"},
            },
        ],
        "cases": [
            {
                "case_id": "style-hit",
                "query": "鸿哥 沟通风格",
                "expected_record_ids": ["style"],
                "forbid_kinds": ["reflection"],
                "topk": 5,
            }
        ],
    }

    report = run_production_recall_eval(runtime, dataset, persist_report=True)

    assert report["ok"] is True
    assert report["report_type"] == "recall_quality_report"
    assert report["legacy_report_type"] == "production_recall_eval"
    assert report["hit_at_1"] == 1.0
    assert report["hit_at_5"] == 1.0
    assert report["false_recall_rate"] == 0.0
    assert report["forbidden_hit_rate"] == 0.0
    assert report["audit_pollution_rate"] == 0.0
    assert report["latency_ms_p95"] >= 0.0
    assert report["persisted"] is True
    assert report["persisted_record_id"]

    stored = runtime.store.get_by_id(report["persisted_record_id"])
    assert stored is not None
    assert stored.kind == "reflection"
    assert stored.meta["report_type"] == "recall_quality_report"
    assert stored.content["report"]["hit_at_5"] == 1.0


def test_default_recall_reports_blocked_operational_counts(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "quality-loop", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    runtime.memory.ingest(
        text="UUMit 交付规则：外部订单先按需求清单验收。",
        memory_type="preference",
        title="UUMit 交付规则",
        scope=scope,
        source="test",
        force_capture=True,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="UUMit old audit",
            summary="UUMit 历史审计日志，不应进入普通召回。",
            detail="UUMit 历史审计日志，不应进入普通召回。",
            content={"text": "UUMit 历史审计日志，不应进入普通召回。"},
            source="openclaw.before_prompt_build",
            scope=scope_ref,
            meta={"report_type": "recall_audit"},
        )
    )

    bundle = runtime.memory.recall(query="UUMit 交付规则 审计", scope=scope, task_context={}, limit=5)

    assert all(item.kind != "reflection" for item in bundle.items)
    blocked_counts = bundle.explanation["recall_filters"]["blocked_counts"]
    assert blocked_counts["audit_record"] >= 1


def test_outcome_without_pattern_id_is_attributed_from_recall_audit(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "quality-loop", "user_id": "darrow"}
    pattern_id = _policy_candidate(runtime, scope=scope, pattern_id="quality-loop-shadow")
    promote_candidate(
        runtime,
        candidate_id=pattern_id,
        scope=scope,
        loop_id="quality_loop",
        eval_result=PASSING_EVAL,
        health={"ok": True},
    )
    scope_ref = ScopeRef.from_dict(scope)
    runtime.store.append(
        RecordEnvelope.create(
            kind="recall_view",
            title="OpenClaw memory injection audit",
            summary="Injected policy suggestion before prompt build",
            detail="Audit record for OpenClaw before_prompt_build memory recall.",
            content={
                "session_id": "sess-quality-loop",
                "policy_suggestion_ids": ["quality-loop-shadow"],
                "selected_records": [{"record_id": "rec-1"}],
                "injection_plan": {"items": []},
            },
            source="openclaw.before_prompt_build",
            scope=scope_ref,
            meta={"session_id": "sess-quality-loop", "policy_suggestion_ids": ["quality-loop-shadow"]},
        )
    )

    for index in range(3):
        event = runtime.record_event(
            {
                "id": f"evt-quality-loop-{index}",
                "source": "test",
                "session_id": "sess-quality-loop",
                "user_phrase": "post promotion hit sample",
                "event_type": "tool_routing",
                "interpreted_intent": "Use audited shadow policy",
                "goal": "Improve policy routing",
                "confidence": 0.9,
            },
            scope=scope,
        )
        outcome = runtime.record_outcome(
            event["id"],
            {
                "outcome": "good",
                "reason": "audited shadow policy improved the task",
                "session_id": "sess-quality-loop",
            },
            scope=scope,
        )

    assert outcome["post_promotion_watch"][0]["status"] == "active"
    assert _intent_pattern(runtime, "quality-loop-shadow")["status"] == "active"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="shadow_observe", limit=10)
    latest = ledger[0]["details"]
    assert latest["decision"] == "active"
    assert latest["audit_record_id"]
    assert latest["outcome_trace_id"] or latest["outcome_event_id"]
    assert latest["candidate_id"]
    assert latest["pattern_id"] == "quality-loop-shadow"


def test_openclaw_e2e_tool_and_cli(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "tool-runtime")

    from eimemory.adapters.openclaw.tools import OpenClawMemoryTools

    tool_result = OpenClawMemoryTools(runtime).memory_e2e_check(
        scope={"agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"},
        query="quality loop e2e",
    )

    assert tool_result["ok"] is True
    assert tool_result["verdict"] == "pass"
    assert tool_result["store"]["ok"] is True
    assert tool_result["recall"]["hit"] is True
    assert tool_result["outcome"]["trace_id"] or tool_result["outcome"]["event_id"]
    assert tool_result["ledger"]["evidence_id"]

    assert cli_main(["doctor", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ok"] is True
    assert doctor["version"]

    assert cli_main(["eval", "openclaw-e2e", "--query", "quality loop e2e cli"]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["ok"] is True
    assert cli_result["verdict"] == "pass"


def _policy_candidate(runtime: Runtime, *, scope: dict, pattern_id: str) -> str:
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="quality_loop",
        learning_goal_id=f"goal-{pattern_id}",
        research_note_id=f"note-{pattern_id}",
        candidate_kind="prompt_policy",
        candidate_patch={
            "id": pattern_id,
            "pattern": "post promotion hit sample",
            "default_event_type": "tool_routing",
            "interpreted_intent": "Use the post-promotion policy only after shadow observation.",
            "execution_policy": ["Prefer the shadow-observed route when it has real hit evidence."],
            "success_criteria": "Three real task observations hit without regression.",
        },
    )
    return distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="quality_loop",
        experiment_id=experiment_id,
        eval_result=PASSING_EVAL,
        promotion_target="prompt_policy",
        summary="Post-promotion policy candidate",
        target_capability="tool.routing",
    )


def _intent_pattern(runtime: Runtime, pattern_id: str) -> dict:
    row = runtime.store.sqlite.conn.execute("SELECT payload_json FROM intent_patterns WHERE id = ?", (pattern_id,)).fetchone()
    assert row is not None
    return json.loads(str(row["payload_json"]))
