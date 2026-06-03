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


def test_governance_snapshot_exposes_autonomous_learning_state(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    runtime.evolution.log_reflection(tag="memory.recall", miss="recall miss", fix="preference first", scope=scope)
    runtime.run_autonomous_learning_cycle(scope=scope, force=True)

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["autonomous_learning"]["loop_count"] == 1
    assert snapshot["autonomous_learning"]["goal_count"] >= 1
    assert snapshot["autonomous_learning"]["candidate_count"] >= 1
