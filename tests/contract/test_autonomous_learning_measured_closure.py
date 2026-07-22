from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_autonomous_learning_failure_gate_preserves_prior_capability_score(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "measured-closure"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="routing drift", fix="prefer memory-first", scope=scope)
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="verified-baseline",
        capability="tool.routing",
        score=0.84,
        evidence_record_ids=["baseline-1", "baseline-2", "baseline-3"],
    )

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

    assert report["capability_score_id"] == ""
    ledger = runtime.learning_ledger(scope=scope, attribute_outcomes=False)
    assert ledger["capabilities"]["tool.routing"]["score"] == 0.84


def test_autonomous_learning_attributes_preexisting_verified_real_outcome_before_synthetic_replays(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "measured-real-outcome"}
    scope_ref = ScopeRef.from_dict(scope)
    source = runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Verified real task receipt",
            summary="External verified business outcome",
            source="openclaw.agent_end",
            scope=scope_ref,
        )
    )
    trace = runtime.record_outcome_trace(
        {
            "source": "openclaw.agent_end",
            "trace_id": "real-uumit-1",
            "idempotency_key": "idem-real-uumit-1",
            "task_type": "operations.uumit",
            "input_summary": "verified real business task",
            "outcome": {"status": "success", "success": True},
            "verifier": {
                "passed": True,
                "method": "real-task",
                "evidence_refs": [source.record_id],
            },
            "capability_contract": {
                "schema_version": "capability_contract.v1",
                "capability": "operations.uumit",
                "case_id": "uumit_requirement_checklist",
                "observations": {
                    "requirement_count": 1,
                    "checklist_complete": True,
                    "acceptance_verified": True,
                },
                "source_record_ids": [source.record_id],
                "checks": [
                    {
                        "name": "real task verified",
                        "passed": True,
                        "evidence_ref": source.record_id,
                    }
                ],
                "probe": False,
            },
        },
        scope=scope,
    )
    assert trace["ok"] is True
    before = runtime.learning_ledger(scope=scope, attribute_outcomes=False)
    assert before["capabilities"]["operations.uumit"]["score"] == 0.0

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

    report = runtime.run_autonomous_learning_cycle(
        scope=scope,
        force=True,
        apply=True,
        max_goals=1,
        max_promotions=1,
    )

    assert report["replay_gate_passed"] is False
    attribution = report["preexisting_outcome_attribution"]
    attributed = attribution["capabilities"]["operations.uumit"]
    assert attributed["evidence_record_ids"] == [trace["record_id"]]
    ledger = runtime.learning_ledger(scope=scope, attribute_outcomes=False)
    capability = ledger["capabilities"]["operations.uumit"]
    assert capability["score"] == 0.82
    assert capability["last_record_id"] in attribution["record_ids"]
    assert trace["record_id"] in capability["evidence_record_ids"]
