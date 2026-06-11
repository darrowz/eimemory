from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.locomo import normalize_locomo_dataset, run_locomo
from eimemory.evaluation.public_benchmarks import run_public_memory_benchmark
from eimemory.evaluation.task_replay import run_real_task_replay


def _scope() -> dict[str, str]:
    return {"agent_id": "hongtu", "workspace_id": "production", "user_id": "darrow"}


def _locomo_dataset() -> dict:
    return {
        "name": "locomo-smoke",
        "scope": _scope(),
        "cases": [
            {
                "id": "coffee-device",
                "question": "Which device should be checked before coffee service?",
                "conversation": [
                    {
                        "turn_id": "coffee-turn",
                        "speaker": "user",
                        "text": "Before coffee service, check the kitchen plug first.",
                    },
                    {
                        "turn_id": "music-turn",
                        "speaker": "user",
                        "text": "For music, verify the speaker is audible.",
                    },
                ],
                "evidence_turn_ids": ["coffee-turn"],
            }
        ],
    }


def _task_replay_dataset() -> dict:
    return {
        "name": "real-task-replay-smoke",
        "schema_version": "real_task_replay.v1",
        "threshold": 0.9,
        "scope": _scope(),
        "seed": [
            {
                "title": "UUMit delivery policy",
                "text": "UUMit delivery should protect milestones and record verification evidence.",
                "memory_type": "policy",
            }
        ],
        "cases": [
            {
                "case_id": "uumit-milestone",
                "source_system": "uumit",
                "task_type": "operations.uumit",
                "query": "UUMit delivery milestone handling",
                "expected_text": ["protect milestones", "verification evidence"],
                "negative_expected_text": ["fake success"],
            }
        ],
    }


def test_locomo_smoke_scores_turn_level_recall(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    normalized = normalize_locomo_dataset(_locomo_dataset())
    report = run_locomo(runtime, normalized, mode="raw", granularity="turn", limit=5)

    assert report["ok"] is True
    assert report["report_type"] == "locomo_eval"
    assert report["sample_count"] == 1
    assert report["recall_at_5"] == 1.0
    assert report["mrr"] == 1.0
    assert report["failure_samples"] == []


def test_locomo_reuses_repeated_conversation_chunks(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = _locomo_dataset()
    for case in dataset["cases"]:
        for turn in case["conversation"]:
            turn["chunk_id"] = turn["turn_id"]
    dataset["cases"].append({**dataset["cases"][0], "id": "coffee-device-repeat"})

    report = run_locomo(runtime, dataset, mode="raw", granularity="turn", limit=5)
    records = runtime.store.list_records(kinds=["raw_chunk"], scope=_scope(), limit=10)

    assert report["sample_count"] == 2
    assert len(records) == 2


def test_public_benchmark_runs_in_isolated_runtime(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_public_memory_benchmark(_locomo_dataset(), suite="locomo", granularity="turn", limit=5)

    assert report["report_type"] == "public_memory_benchmark"
    assert report["isolated_state"] is True
    assert report["suite"] == "locomo"
    assert report["metrics"]["r_at_5"] == 1.0
    assert runtime.store.list_records(scope=_scope(), limit=10) == []


def test_real_task_replay_uses_temp_seed_state(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_real_task_replay(runtime, _task_replay_dataset())

    assert report["ok"] is True
    assert report["schema_version"] == "real_task_replay.v1"
    assert report["pass_rate"] == 1.0
    assert report["failure_samples"] == []
    assert runtime.store.list_records(scope=_scope(), limit=10) == []
    assert runtime.store.list_records(kinds=["replay_result"], scope=_scope(), limit=10) == []


def test_real_task_replay_can_persist_report_record(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_real_task_replay(runtime, _task_replay_dataset(), persist_report=True)
    records = runtime.store.list_records(kinds=["replay_result"], scope=_scope(), limit=10)

    assert report["pass_rate"] == 1.0
    assert report["verdict"] == "pass"
    assert len(records) == 1
    record = records[0]
    assert record.kind == "replay_result"
    assert record.source == "eimemory.real_task_replay"
    assert record.meta["report_type"] == "real_task_replay"
    assert record.meta["replay_source"] == "real_task_replay"
    assert record.meta["schema_version"] == "real_task_replay.v1"
    assert record.meta["verdict"] == "pass"
    assert record.meta["pass_rate"] == 1.0
    assert record.meta["sample_count"] == 1
    assert record.meta["fail_count"] == 0
    assert record.meta["threshold"] == 0.9
    assert record.meta["scope"] == report["scope"]
    assert record.content["report"]["pass_rate"] == 1.0


def test_cli_eval_locomo_and_task_replay_write_reports(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    locomo_path = tmp_path / "locomo.json"
    replay_path = tmp_path / "replay.json"
    locomo_out = tmp_path / "locomo-report.json"
    replay_out = tmp_path / "replay-report.json"
    locomo_path.write_text(json.dumps(_locomo_dataset(), ensure_ascii=False), encoding="utf-8")
    replay_path.write_text(json.dumps(_task_replay_dataset(), ensure_ascii=False), encoding="utf-8")

    assert cli_main(["eval", "locomo", str(locomo_path), "--output", str(locomo_out)]) == 0
    locomo_printed = json.loads(capsys.readouterr().out)
    assert locomo_printed["output"] == str(locomo_out)
    assert json.loads(locomo_out.read_text(encoding="utf-8"))["report_type"] == "locomo_eval"

    assert cli_main(["eval", "task-replay", str(replay_path), "--output", str(replay_out)]) == 0
    replay_printed = json.loads(capsys.readouterr().out)
    assert replay_printed["output"] == str(replay_out)
    assert json.loads(replay_out.read_text(encoding="utf-8"))["report_type"] == "real_task_replay"


def test_cli_eval_task_replay_can_persist_and_write_report(tmp_path, monkeypatch, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(runtime_root))
    replay_path = tmp_path / "replay.json"
    replay_out = tmp_path / "replay-report.json"
    replay_path.write_text(json.dumps(_task_replay_dataset(), ensure_ascii=False), encoding="utf-8")

    assert (
        cli_main(["eval", "task-replay", str(replay_path), "--persist-report", "--output", str(replay_out)])
        == 0
    )
    replay_printed = json.loads(capsys.readouterr().out)
    runtime = Runtime.create(root=runtime_root)
    records = runtime.store.list_records(kinds=["replay_result"], scope=_scope(), limit=10)

    assert replay_printed["output"] == str(replay_out)
    assert json.loads(replay_out.read_text(encoding="utf-8"))["report_type"] == "real_task_replay"
    assert len(records) == 1
    assert records[0].meta["verdict"] == "pass"
    assert records[0].meta["pass_rate"] == 1.0
