from __future__ import annotations

from eimemory.api.runtime import Runtime


def test_autonomous_learning_failure_gate_records_measured_eval_and_low_score(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "measured-closure"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="routing drift", fix="prefer memory-first", scope=scope)

    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.build_replay_dataset",
        lambda *_args, **_kwargs: {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 1,
            "cases": [{"case_id": "case_1", "query": "sample query", "task_type": "brain.respond"}],
        },
    )
    monkeypatch.setattr(
        runtime,
        "run_real_task_replay",
        lambda *_args, **_kwargs: {
            "ok": True,
            "report_type": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "verdict": "pass",
            "pass_rate": "bad",
            "threshold": 0.6,
            "sample_count": "bad",
        },
    )

    report = runtime.run_autonomous_learning_cycle(scope=scope, force=True, apply=True, max_goals=1, max_promotions=1)

    assert report["ok"] is True
    assert report["replay_gate_passed"] is False
    assert report["eval_verdict"] == "fail"
    assert report["candidate_ids"] == []

    eval_record = runtime.store.get_by_id(report["eval_record_id"], scope=scope)
    assert eval_record is not None
    assert eval_record.content["eval_suite"]["measurement_source"] == "autonomous_learning_gates"
    assert eval_record.content["scores"]["capability"] == 0.0
    assert "real_task_replay_no_samples" in eval_record.content["blocked_reasons"]

    ledger = runtime.learning_ledger(scope=scope, attribute_outcomes=False)
    scored = [item for item in ledger["capabilities"].values() if item["last_record_id"] == report["capability_score_id"]]
    assert scored
    assert scored[0]["score"] == 0.0
