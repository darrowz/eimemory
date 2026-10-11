"""Controlled recall results verify durable diagnosis, not production quality."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.scheduler.result_contract import nightly_result_diagnostics


@pytest.mark.parametrize("failure", ["known_item_miss_with_return", "no_answer_return", "empty_miss"])
def test_reopened_quality_receipt_preserves_failure_identity_and_rate(tmp_path, monkeypatch, failure):
    scope = {"agent_id": "receipt-test", "workspace_id": "quality", "user_id": "operator"}
    runtime = Runtime.create(root=tmp_path)
    try:
        targets = [RecordEnvelope.create(
            kind="memory", title=f"PRIVATE_TITLE_{i}", summary=f"PRIVATE_SUMMARY_{i}",
            content={"text": f"PRIVATE_BODY_{i}"}, scope=ScopeRef.from_dict(scope),
            source="operator.note", meta={"memory_type": "fact"},
        ) for i in range(11)]
        for target in targets:
            runtime.store.append(target)
        cases = [{"case_id": f"case-{i}", "query": f"PRIVATE_QUERY_{i}",
                  "expected_record_ids": [target.record_id], "topk": 5}
                 for i, target in enumerate(targets[:10])]
        if failure == "no_answer_return":
            cases[-1].pop("expected_record_ids")
            cases[-1]["no_answer"] = True

        def recall(*, query, **kwargs):
            index = next(i for i, case in enumerate(cases) if case["query"] == query)
            items = [targets[index]] if index < 9 else [] if failure == "empty_miss" else [targets[-1]]
            return RecallBundle(items=items, rules=[], reflections=[], confidence=1.0,
                                next_action_hint="", explanation={})

        monkeypatch.setattr(runtime.memory, "recall", recall)
        report = run_production_recall_eval(runtime, {
            "name": "receipt-regression", "scope": scope,
            "evaluation_contract": "known_item_smoke.v1", "cases": cases,
        }, seed=False, persist_report=True)
        assert report["persisted"] is True
        receipt_id = report["persisted_record_id"]
    finally:
        runtime.close()

    reopened = Runtime.create(root=tmp_path)
    try:
        receipt = reopened.store.get_by_id(receipt_id)
        assert receipt is not None
        saved = receipt.content["report"]
        assert (saved["pass_count"], saved["fail_count"]) == (9, 1)
        assert saved["false_recall_denominator"] == saved["sample_count"] == 10
        assert saved["false_recall_count"] == (0 if failure == "empty_miss" else 1)
        assert saved["false_recall_rate"] == (0.0 if failure == "empty_miss" else 0.1)
        breakdown = saved["false_recall_breakdown"]
        assert breakdown["known_item_miss_with_return_count"] == (failure == "known_item_miss_with_return")
        assert breakdown["no_answer_return_count"] == (failure == "no_answer_return")
        assert breakdown["no_answer_sample_count"] == (failure == "no_answer_return")
        failed = [sample for sample in saved["samples"] if sample["passed"] is False]
        assert len(failed) == 1
        sample = failed[0]
        assert sample["case_id"] == "case-9"
        assert sample["expected_record_ids"] == ([] if failure == "no_answer_return" else [targets[9].record_id])
        assert sample["returned_record_ids"] == ([] if failure == "empty_miss" else [targets[-1].record_id])
        assert sample["rank"] == 0 and sample["matched_expected"] is False
        assert sample["empty"] == (failure == "empty_miss")
        assert sample["false_recall_reason"] == ("" if failure == "empty_miss" else failure)
        assert "PRIVATE_" not in json.dumps(saved)

        gate = saved["quality_gate"]
        nightly = nightly_result_diagnostics({"recall_quality_gate": gate}, [])
        assert gate["thresholds"]["false_recall_rate"] == 0.05
        assert gate["ok"] is False and nightly["recall_quality_accepted"] is False
        assert saved["recall_quality_evidence"]["release_authority_verified"] is False
        if failure == "empty_miss":
            assert gate["blocked_reason"] == "recall_quality_evidence_incomplete"
            assert nightly["execution_ok"] is True
        else:
            assert gate["blocked_reason"] == "recall_quality_gate_failed"
            assert gate["blocking_metrics"]["false_recall_rate"] == {
                "actual": 0.1, "threshold": 0.05, "operator": "<="}
            assert nightly["execution_ok"] is False
            assert nightly["failed_steps"] == ["recall_quality_gate"]
    finally:
        reopened.close()
