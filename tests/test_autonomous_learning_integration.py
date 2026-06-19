from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.snapshot import build_governance_snapshot
from eimemory.scheduler.jobs import run_nightly_jobs


def test_nightly_jobs_skip_autonomous_learning_by_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.delenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", raising=False)
    monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda dataset, *, emit_incidents=False: {"ok": True, "pass_rate": 1.0, "passed_threshold": True, "fail_count": 0, "name": "stub"})

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})

    assert report["autonomous_learning"]["ok"] is True
    assert report["autonomous_learning"]["enabled"] is False
    assert report["autonomous_learning"]["learning_skipped_reason"] == "autonomous_learning_disabled"


def test_nightly_jobs_include_autonomous_learning_summary(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_DRY_RUN", "0")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_APPLY", "0")
    monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda dataset, *, emit_incidents=False: {"ok": True, "pass_rate": 1.0, "passed_threshold": True, "fail_count": 0, "name": "stub"})
    _force_real_task_replay_pass(runtime, monkeypatch)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="bad route", fix="memory first", scope=scope)

    report = run_nightly_jobs(runtime, scope=scope)

    assert report["autonomous_learning"]["ok"] is True
    assert report["autonomous_learning"]["enabled"] is True
    assert report["autonomous_learning"]["goal_count"] >= 1
    assert report["autonomous_learning"]["candidate_count"] >= 1
    assert report["autonomous_learning"]["applied_count"] == 0
    assert report["autonomous_learning_daily_report"]["ok"] is True
    assert report["autonomous_learning_daily_report"]["persisted"] is True
    assert report["autonomous_learning_daily_report"]["summary"]
    assert report["autonomous_learning_dashboard"]["report_type"] == "autonomous_learning_daily_dashboard"
    assert report["autonomous_learning_dashboard"]["period_type"] == "daily"


def test_nightly_jobs_forward_max_promotion_budget(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: dict[str, int | bool] = {}
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_DRY_RUN", "0")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_APPLY", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS", "0")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_NETWORK", "1")
    monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda dataset, *, emit_incidents=False: {"ok": True, "pass_rate": 1.0, "passed_threshold": True, "fail_count": 0, "name": "stub"})

    def fake_learning(**kwargs):
        calls["apply"] = kwargs["apply"]
        calls["max_promotions"] = kwargs["max_promotions"]
        calls["allow_network"] = kwargs["allow_network"]
        return {"ok": True, "dry_run": False, "apply": True, "goal_count": 1, "candidate_ids": ["candidate_1"], "promotions": [{"applied": False}]}

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_learning)

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})

    assert report["autonomous_learning"]["ok"] is True
    assert calls["apply"] is True
    assert calls["max_promotions"] == 0
    assert calls["allow_network"] is True
    assert report["autonomous_learning"]["max_promotions"] == 0
    assert report["autonomous_learning"]["network_research_enabled"] is True
    assert report["autonomous_learning"]["applied_count"] == 0


def test_nightly_jobs_allows_network_research_by_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: dict[str, bool] = {}
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_DRY_RUN", "0")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_APPLY", "0")
    monkeypatch.delenv("EIMEMORY_AUTONOMOUS_LEARNING_NETWORK", raising=False)
    monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda dataset, *, emit_incidents=False: {"ok": True, "pass_rate": 1.0, "passed_threshold": True, "fail_count": 0, "name": "stub"})

    def fake_learning(**kwargs):
        calls["allow_network"] = kwargs["allow_network"]
        return {
            "ok": True,
            "dry_run": False,
            "apply": False,
            "goal_count": 1,
            "candidate_ids": [],
            "promotions": [],
            "network_research": {"enabled": kwargs["allow_network"], "hypothesis_count": 0, "error_count": 0},
        }

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_learning)

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})

    assert calls["allow_network"] is True
    assert report["autonomous_learning"]["network_research_enabled"] is True


def test_governance_snapshot_exposes_autonomous_learning_state(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    _force_real_task_replay_pass(runtime, monkeypatch)
    runtime.evolution.log_reflection(tag="memory.recall", miss="recall miss", fix="preference first", scope=scope)
    runtime.run_autonomous_learning_cycle(scope=scope, force=True)

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["autonomous_learning"]["loop_count"] == 1
    assert snapshot["autonomous_learning"]["goal_count"] >= 1
    assert snapshot["autonomous_learning"]["candidate_count"] >= 1


def test_runtime_learning_cycle_does_not_persist_dashboard_side_effect(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="memory.recall", miss="recall miss", fix="preference first", scope=scope)

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True)

    assert report["ok"] is True
    dashboards = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=100)
        if str(record.meta.get("report_type") or "") in {"autonomous_learning_daily_dashboard", "autonomous_learning_weekly_dashboard"}
    ]
    assert dashboards == []


def test_autonomous_learning_cycle_returns_real_task_replay_report(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="routing drift", fix="prefer memory-first", scope=scope)

    replay_dataset_calls: list[dict] = []

    def fake_build_replay_dataset(_runtime, *, scope, limit=50, persist=True, loop_id=""):
        replay_dataset_calls.append({"scope": scope, "limit": limit, "persist": persist, "loop_id": loop_id})
        return {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 2,
            "correction_count": 0,
            "persisted_record_id": "replay_dataset_record",
            "cases": [
                {"case_id": "case_1", "query": "sample query", "task_type": "brain.respond"},
                {"case_id": "case_2", "query": "secondary query", "task_type": "brain.respond"},
            ],
        }

    replay_calls: list[dict] = []

    def fake_run_real_task_replay(dataset, *, seed=False, persist_report=False):
        replay_calls.append(
            {
                "seed": seed,
                "persist_report": persist_report,
                "case_count": len(dataset.get("cases") or []),
                "seed_count": len(dataset.get("seed") or []),
                "threshold": dataset.get("threshold"),
            }
        )
        return {
            "ok": True,
            "report_type": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "verdict": "pass",
            "pass_rate": 1.0,
            "pass_count": 2,
            "fail_count": 0,
            "persisted_record_id": "replay_report_record",
        }

    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.build_replay_dataset",
        fake_build_replay_dataset,
    )
    monkeypatch.setattr(runtime, "run_real_task_replay", fake_run_real_task_replay)

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False)

    assert replay_dataset_calls
    assert replay_calls
    assert replay_calls[0]["persist_report"] is True
    assert report["ok"] is True
    assert replay_calls[0]["seed"] is True
    assert replay_calls[0]["seed_count"] == 2
    assert replay_calls[0]["threshold"] == 0.6
    assert report["real_task_replay"]["ok"] is True
    assert report["real_task_replay"]["report_type"] == "real_task_replay"


def test_autonomous_learning_cycle_can_attach_web_scout_evidence_when_network_enabled(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="knowledge.source", miss="research stale", fix="check current web evidence", scope=scope)
    _force_real_task_replay_pass(runtime, monkeypatch)
    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.generate_learning_goals",
        lambda *_args, **_kwargs: [
            {
                "goal_type": "maintenance",
                "title": "Refresh source policy",
                "question": "Which current sources should shape memory research policy?",
                "success_criteria": "Promote current source evidence only when it can improve source quality or replay coverage.",
                "authority_tier": "L0",
                "priority": 0.7,
                "target_capability": "knowledge.source",
                "semantic_key": "test_knowledge_source",
            }
        ],
    )
    scout_calls: list[dict] = []

    def fake_scout_web_learning(*, scope, urls=None, evidence=None, timeout_seconds=8):
        scout_calls.append(
            {
                "scope": scope,
                "urls": list(urls or []),
                "evidence": list(evidence or []),
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "ok": True,
            "source": "web_learning_scout",
            "reflection_record_id": "web_ref_1",
            "hypothesis_count": 1,
            "hypotheses": [
                {
                    "source_url": "https://example.com/current-memory-research",
                    "candidate_policy": {
                        "title": "Current memory research",
                        "policy_update": "Use fresh retrieval evidence before changing memory policy.",
                    },
                    "replay_hints": [
                        {
                            "query": "current memory research",
                            "expected_text": ["fresh retrieval evidence"],
                        }
                    ],
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(runtime, "scout_web_learning", fake_scout_web_learning)

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False, allow_network=True)
    research_note = runtime.store.get_by_id(report["research_note_id"], scope=scope)
    evidence = research_note.content["evidence"]

    assert scout_calls
    assert any(item["kind"] == "web_learning_scout" and item["tier"] == "T3" for item in evidence)
    assert report["network_research"]["enabled"] is True
    assert report["network_research"]["hypothesis_count"] == 1
    output_gate = report["network_research"]["output_gate"]
    assert output_gate["decision"] == "actionable"
    assert "source_score" in output_gate["landing_targets"]
    assert "replay" in output_gate["landing_targets"]
    gate_record = runtime.store.get_by_id(output_gate["summary_record_id"], scope=scope)
    assert gate_record is not None
    assert gate_record.meta["report_type"] == "network_learning_output_gate"
    assert gate_record.content["decision"] == "actionable"


def test_autonomous_learning_cycle_uses_network_research_by_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="knowledge.source", miss="research stale", fix="check current web evidence", scope=scope)
    _force_real_task_replay_pass(runtime, monkeypatch)
    scout_calls: list[dict] = []

    def fake_scout_web_learning(*, scope, urls=None, evidence=None, timeout_seconds=8):
        scout_calls.append({"scope": scope, "urls": list(urls or []), "timeout_seconds": timeout_seconds})
        return {
            "ok": True,
            "source": "web_learning_scout",
            "reflection_record_id": "web_ref_1",
            "hypothesis_count": 1,
            "hypotheses": [
                {
                    "source_url": "https://example.com/default-network",
                    "candidate_policy": {
                        "title": "Default network research",
                        "policy_update": "Default autonomous learning should gather current web evidence.",
                    },
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(runtime, "scout_web_learning", fake_scout_web_learning)

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False)

    assert scout_calls
    assert report["network_research"]["enabled"] is True
    assert report["network_research"]["hypothesis_count"] == 1


def test_autonomous_learning_cycle_adds_local_fallback_when_default_network_has_no_hypotheses(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    _force_real_task_replay_pass(runtime, monkeypatch)

    monkeypatch.setattr(
        runtime,
        "scout_web_learning",
        lambda **_: {
            "ok": True,
            "source": "web_learning_scout",
            "reflection_record_id": "web_ref_empty",
            "hypothesis_count": 0,
            "hypotheses": [],
            "errors": [],
        },
    )

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False)
    research_note = runtime.store.get_by_id(report["research_note_id"], scope=scope)
    evidence_tiers = {item["tier"] for item in research_note.content["evidence"]}

    assert report["ok"] is True
    assert report["network_research"]["enabled"] is True
    assert report["network_research"]["output_gate"]["decision"] == "skipped"
    assert report["network_research"]["output_gate"]["reason"] == "no_web_hypotheses"
    assert report["network_research"]["output_gate"]["landing_targets"] == []
    assert "T2" in evidence_tiers


def test_autonomous_learning_network_output_gate_can_keep_web_learning_as_reference_summary(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    _force_real_task_replay_pass(runtime, monkeypatch)

    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.generate_learning_goals",
        lambda *_args, **_kwargs: [
            {
                "goal_type": "maintenance",
                "title": "Refresh operational notes",
                "question": "Can recent external notes inform operations without changing policy?",
                "success_criteria": "Keep useful background as reference when no durable artifact is justified.",
                "authority_tier": "L0",
                "priority": 0.4,
                "target_capability": "operations.context",
                "semantic_key": "test_ops_context",
            }
        ],
    )
    monkeypatch.setattr(
        runtime,
        "scout_web_learning",
        lambda **_: {
            "ok": True,
            "source": "web_learning_scout",
            "reflection_record_id": "web_ref_ops",
            "hypothesis_count": 1,
            "hypotheses": [
                {
                    "source_url": "https://example.com/background-only",
                    "candidate_policy": {
                        "title": "Background only",
                        "policy_update": "Interesting context, but not enough to change a rule or patch code.",
                    },
                }
            ],
            "errors": [],
        },
    )

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False, allow_network=True)

    output_gate = report["network_research"]["output_gate"]
    assert output_gate["decision"] == "summary_only"
    assert output_gate["landing_targets"] == ["summary"]
    assert output_gate["reason"] == "no_actionable_landing_target"
    gate_record = runtime.store.get_by_id(output_gate["summary_record_id"], scope=scope)
    assert gate_record is not None
    assert gate_record.content["decision"] == "summary_only"
    assert gate_record.content["landing_targets"] == ["summary"]


def test_autonomous_learning_cycle_reports_skipped_real_task_replay_on_failure(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="routing drift", fix="prefer memory-first", scope=scope)

    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.build_replay_dataset",
        lambda *_args, **_kwargs: {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 1,
            "correction_count": 0,
            "persisted_record_id": "replay_dataset_record",
            "cases": [{"case_id": "case_1", "query": "sample query", "task_type": "brain.respond"}],
        },
    )
    monkeypatch.setattr(
        runtime,
        "run_real_task_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("replay unavailable")),
    )

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=False)

    assert report["ok"] is True
    assert report["real_task_replay"]["ok"] is False
    assert report["real_task_replay"]["replay_skipped_reason"] == "real_task_replay_failed"
    assert report["replay_gate_passed"] is False
    assert report["candidate_ids"] == []
    assert report["promotions"] == []


def test_autonomous_learning_required_env_fails_when_disabled(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.delenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", raising=False)
    monkeypatch.setenv("EIMEMORY_REQUIRE_AUTONOMOUS_LEARNING", "1")
    monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda dataset, *, emit_incidents=False: {"ok": True, "pass_rate": 1.0, "passed_threshold": True, "fail_count": 0, "name": "stub"})

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})

    assert report["autonomous_learning"]["ok"] is False
    assert report["autonomous_learning"]["configured"] is True
    assert report["autonomous_learning"]["requires_enable_env"] == "EIMEMORY_AUTONOMOUS_LEARNING_ENABLED=1"
    assert report["autonomous_learning"]["learning_skipped_reason"] == "autonomous_learning_required_but_disabled"


def _force_real_task_replay_pass(runtime: Runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.build_replay_dataset",
        lambda *_args, **_kwargs: {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 1,
            "correction_count": 1,
            "persisted_record_id": "replay_dataset_record",
            "cases": [{"case_id": "case_1", "query": "sample query", "task_type": "brain.respond", "expected_text": ["expected"]}],
        },
    )
    monkeypatch.setattr(
        runtime,
        "run_real_task_replay",
        lambda dataset, *, seed=False, persist_report=False: {
            "ok": True,
            "report_type": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "verdict": "pass",
            "pass_rate": 1.0,
            "threshold": dataset.get("threshold", 0.6),
            "sample_count": len(dataset.get("cases") or []),
            "pass_count": len(dataset.get("cases") or []),
            "fail_count": 0,
        },
    )
