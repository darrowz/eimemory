from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main


def test_runtime_eval_seeds_records_and_reports_recall_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "memory-smoke",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "task_type": "brain.respond",
        "seed": [
            {
                "title": "Official channel",
                "text": "Feishu is the official communication channel for Hongtu operator coordination.",
                "memory_type": "decision",
            },
            {
                "title": "Skill boundary",
                "text": "eiskills owns executable skill assets while eimemory owns experience evidence.",
                "memory_type": "fact",
            },
        ],
        "cases": [
            {
                "id": "official-channel",
                "query": "official communication channel",
                "expect_any_title": ["Official channel"],
                "limit": 3,
            },
            {
                "id": "skill-memory-boundary",
                "query": "who owns skill assets and experience evidence",
                "expect_any_text": ["eiskills owns executable skill assets"],
                "limit": 3,
            },
            {
                "id": "missing",
                "query": "unrelated missing memory",
                "expect_any_title": ["Definitely absent"],
                "limit": 3,
            },
        ],
    }

    report = runtime.run_evaluation(dataset)

    assert report["ok"] is True
    assert report["name"] == "memory-smoke"
    assert report["sample_count"] == 3
    assert report["hit_count"] == 2
    assert report["miss_count"] == 1
    assert report["pass_rate"] == 0.667
    assert report["mrr"] > 0.0
    assert report["precision_at_k"] > 0.0
    assert len(report["seeded_record_ids"]) == 2
    assert report["misses"][0]["case_id"] == "missing"
    assert report["samples"][0]["retrieval_mode"] in {"hybrid", "hybrid_vector"}


def test_runtime_eval_handles_list_dataset_and_invalid_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.run_evaluation([None, {"query": ""}], scope={"agent_id": "hongtu", "workspace_id": "embodied"})

    assert report["name"] == "list_dataset"
    assert report["sample_count"] == 2
    assert report["hit_count"] == 0
    assert [sample["error"] for sample in report["samples"]] == ["invalid_case", "empty_query"]


def test_cli_eval_run_writes_report_file(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(
        json.dumps(
            {
                "name": "cli-eval",
                "seed": [
                    {
                        "title": "CLI eval memory",
                        "text": "Evaluation CLI should run deterministic memory recall checks.",
                        "memory_type": "fact",
                    }
                ],
                "cases": [
                    {
                        "query": "deterministic memory recall checks",
                        "expect_any_title": ["CLI eval memory"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert cli_main(["eval", "run", str(dataset_path), "--output", str(output_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed["output"] == str(output_path)
    assert written["name"] == "cli-eval"
    assert written["pass_rate"] == 1.0
    assert written["seeded"] is True


def test_cli_eval_run_rejects_invalid_dataset_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "broken.json"
    dataset_path.write_text("{", encoding="utf-8")

    assert cli_main(["eval", "run", str(dataset_path)]) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is False
    assert report["error"] == "invalid_dataset_json"
