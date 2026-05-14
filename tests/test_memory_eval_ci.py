from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.contracts import normalize_memory_eval_suite
from eimemory.evaluation.metrics import (
    binary_pass_rate,
    mean_reciprocal_rank,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
)


def test_memory_eval_metric_primitives_are_deterministic() -> None:
    returned = ["a", "b", "c", "d"]
    expected = {"b", "x"}

    assert recall_at_k(returned, expected, k=3) == 0.5
    assert precision_at_k(returned, expected, k=3) == 0.333
    assert mean_reciprocal_rank([0, 2, 0, 1]) == 0.375
    assert binary_pass_rate([True, False, True]) == 0.667
    assert ndcg_at_k(returned, expected, k=3) == 0.387
    assert percentile([10, 20, 30, 40], 95) == 40.0


def test_memory_eval_suite_normalization_sets_defaults() -> None:
    suite = normalize_memory_eval_suite(
        {
            "name": "memory-ci",
            "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
            "threshold": 0.75,
            "cases": [
                {
                    "id": "usage-case",
                    "phase": "usage",
                    "query": "official channel",
                    "expect_any_text": ["Feishu"],
                }
            ],
        }
    )

    assert suite["schema_version"] == 2
    assert suite["name"] == "memory-ci"
    assert suite["threshold"] == 0.75
    assert suite["cases"][0]["phase"] == "usage"
    assert suite["cases"][0]["limit"] == 5
    assert suite["cases"][0]["case_id"] == "usage-case"


def test_memory_eval_ci_scores_usage_hallucination_and_all_phases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    dataset = {
        "name": "memory-ci-smoke",
        "scope": scope,
        "threshold": 0.9,
        "seed": [
            {
                "title": "Official channel",
                "text": "Feishu is the official communication channel for coordination.",
                "memory_type": "decision",
            },
            {
                "title": "Current destination",
                "text": "The current travel destination is Chongqing, replacing the older Chengdu plan.",
                "memory_type": "fact",
            },
        ],
        "cases": [
            {
                "id": "extract-channel",
                "phase": "extraction",
                "input_text": "Use Feishu as the official channel.",
                "expect_memory_type": "decision",
                "expect_any_text": ["Feishu"],
            },
            {
                "id": "update-destination",
                "phase": "update",
                "query": "current travel destination",
                "expect_current_text": ["Chongqing"],
                "expect_any_text": ["Chongqing"],
                "limit": 3,
            },
            {
                "id": "usage-channel",
                "phase": "usage",
                "query": "official communication channel",
                "expect_any_title": ["Official channel"],
                "limit": 3,
            },
            {
                "id": "consistency-fact",
                "phase": "consistency",
                "query": "travel destination",
                "expect_any_text": ["Chongqing"],
                "limit": 3,
            },
            {
                "id": "temporal-channel",
                "phase": "temporal",
                "query": "current travel destination",
                "expect_any_text": ["Chongqing"],
                "limit": 3,
            },
            {
                "id": "implicit-risk",
                "phase": "implicit",
                "query": "What is the official channel?",
                "expect_any_text": ["Feishu"],
                "forbid_any_text": ["Feishu"],
            },
        ],
    }

    report = runtime.run_memory_eval_ci(dataset)

    assert report["ok"] is True
    assert report["schema_version"] == 2
    assert report["report_type"] == "memory_eval_ci"
    assert report["sample_count"] == 6
    assert report["pass_count"] == 5
    assert report["fail_count"] == 1
    assert report["passed_threshold"] is False
    assert report["phase_scores"]["extraction"]["pass_rate"] == 1.0
    assert report["phase_scores"]["implicit"]["pass_rate"] == 0.0
    assert report["phase_scores"]["usage"]["pass_rate"] == 1.0
    assert report["phase_scores"]["usage"]["recall_at_k"] == 1.0
    assert report["phase_scores"]["usage"]["precision_at_k"] == 1.0
    assert report["efficiency"]["case_count"] == 6
    assert len(report["failures"]) == 1
    assert report["failures"][0]["phase"] == "implicit"
    assert report["failures"][0]["hallucinated"] is True


def test_memory_eval_ci_can_emit_incidents_for_failed_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    dataset = {
        "name": "memory-ci-incident",
        "scope": scope,
        "threshold": 0.9,
        "cases": [
            {
                "id": "missing-memory",
                "phase": "usage",
                "query": "unknown query intentionally missing",
                "expect_any_text": ["definitely not present"],
                "repair_hint": "Prefer storing and exposing this preference.",
                "forbid_any_text": ["Definitely not"],
            }
        ],
    }

    report = runtime.run_memory_eval_ci(dataset, emit_incidents=True)

    assert report["passed_threshold"] is False
    assert len(report["incident_record_ids"]) == 1

    incidents = runtime.store.list_records(kinds=["incident"], scope=scope, limit=10)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.meta.get("eval_failure") is True
    assert incident.meta.get("eval_phase") == "usage"
    assert incident.meta.get("repair_hint") == "Prefer storing and exposing this preference."
    assert isinstance(incident.meta.get("suggested_replay_dataset"), list)
    assert incident.meta["suggested_replay_dataset"][0]["query"] == "unknown query intentionally missing"
    assert incident.meta["suggested_replay_dataset"][0]["expect_any_text"] == ["definitely not present"]


def test_cli_eval_ci_writes_report_and_returns_nonzero_below_threshold(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(
        json.dumps(
            {
                "name": "cli-memory-ci",
                "threshold": 1.0,
                "cases": [
                    {
                        "id": "missing",
                        "phase": "usage",
                        "query": "missing preference",
                        "expect_any_text": ["not present"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(["eval", "ci", str(dataset_path), "--emit-incidents", "--output", str(output_path)])
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert printed["output"] == str(output_path)
    assert written["report_type"] == "memory_eval_ci"
    assert written["passed_threshold"] is False
    assert written["pass_rate"] < 1.0
    assert len(written["incident_record_ids"]) == 1
