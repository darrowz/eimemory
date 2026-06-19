"""Tests for eimemory.autonomous.exp_log — compounding experiment log JSONL."""
import json
import tempfile
from pathlib import Path

from eimemory.autonomous.exp_log import ExpLog, ExpLogEntry, entry_from_experiment_result


def test_log_writes_and_reads():
    with tempfile.TemporaryDirectory() as tmp:
        log = ExpLog(Path(tmp) / "exp.jsonl")
        log.append(ExpLogEntry(
            hypothesis="h1", kept=True, elapsed=10.0,
            primary_metric_before=0.6, primary_metric_after=0.65,
        ))
        entries = log.read_all()
        assert len(entries) == 1
        assert entries[0].kept is True


def test_log_recent_kept_count():
    with tempfile.TemporaryDirectory() as tmp:
        log = ExpLog(Path(tmp) / "exp.jsonl")
        for i in range(10):
            log.append(ExpLogEntry(
                hypothesis=f"h{i}", kept=(i % 3 == 0), elapsed=10.0,
                primary_metric_before=0.6, primary_metric_after=0.6 + i * 0.01,
            ))
        kept_recent = log.recent_kept(n=5)
        # Last 5 entries are i=5..9; kept ones are i=6 and i=9.
        assert len([e for e in kept_recent if e.kept]) == 2


def test_log_reads_new_runner_outcome_schema_without_kept_flag():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "exp.jsonl"
        path.write_text(
            json.dumps(
                {
                    "experiment_id": "exp-1",
                    "hypothesis": {"text": "reduce recall misses"},
                    "outcome": "kept",
                    "duration_seconds": 3.0,
                    "baseline_value": 0.40,
                    "candidate_value": 0.45,
                    "metric_name": "recall_view.hit_at_1",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entries = ExpLog(path).read_all()

        assert len(entries) == 1
        assert entries[0].experiment_id == "exp-1"
        assert entries[0].kept is True
        assert entries[0].primary_metric_before == 0.40
        assert entries[0].primary_metric_after == 0.45


def test_entry_from_experiment_result_uses_outcome_when_kept_missing():
    entry = entry_from_experiment_result(
        {
            "experiment_id": "exp-2",
            "hypothesis": "h",
            "outcome": "discarded",
            "duration_seconds": 2.0,
            "baseline_value": 0.50,
            "candidate_value": 0.49,
        }
    )

    assert entry.kept is False
    assert entry.outcome == "discarded"
