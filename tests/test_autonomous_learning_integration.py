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
