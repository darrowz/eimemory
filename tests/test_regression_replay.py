from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.evaluation.regression_replay import mistake_to_replay_case, run_regression_replay
from eimemory.governance.replay_dataset import build_replay_dataset
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_version_mistake_replay_case_targets_evidence_query_first() -> None:
    case = mistake_to_replay_case(
        "version_answer_wrong",
        "What version of EIMemory is currently installed?",
        ["query version evidence first", "EIMemory 1.4.4"],
    )

    assert case["mistake_type"] == "version_answer_wrong"
    assert case["target_capability"] == "evidence.query_first"
    assert case["query"] == "What version of EIMemory is currently installed?"
    assert case["expected_text"] == ["query version evidence first", "EIMemory 1.4.4"]


def test_regression_replay_fails_when_expected_text_missing() -> None:
    report = run_regression_replay(
        [
            {
                "case_id": "case_missing_version",
                "query": "What version is installed?",
                "expected_text": ["EIMemory 1.4.4", "query version evidence first"],
            }
        ],
        {"case_missing_version": "I think it is probably 1.4.3."},
    )

    assert report["ok"] is True
    assert report["verdict"] == "fail"
    assert report["pass_count"] == 0
    assert report["fail_count"] == 1
    assert report["samples"][0]["passed"] is False
    assert report["samples"][0]["missing_expected_text"] == ["EIMemory 1.4.4", "query version evidence first"]


def test_replay_dataset_includes_persisted_regression_replay_case(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    scope_ref = ScopeRef.from_dict(scope)
    case = mistake_to_replay_case(
        "version_answer_wrong",
        "Answer the installed EIMemory version after checking evidence.",
        ["query version evidence first", "EIMemory 1.4.4", "do not guess the version"],
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Regression replay case: version answer",
            summary="Known version answer mistake must be replayed.",
            content=case,
            scope=scope_ref,
            source="unit.test",
            meta={"report_type": "regression_replay_case"},
        )
    )

    report = build_replay_dataset(runtime, scope=scope, limit=10, persist=False)

    assert report["case_count"] == 1
    assert report["cases"][0]["source"] == "regression_replay_case"
    assert report["cases"][0]["target_capability"] == "evidence.query_first"
    assert report["cases"][0]["expected_text"][:3] == [
        "query version evidence first",
        "EIMemory 1.4.4",
        "do not guess the version",
    ]
