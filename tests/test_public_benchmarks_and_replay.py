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
