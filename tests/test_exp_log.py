"""Tests for eimemory.autonomous.exp_log — compounding experiment log JSONL."""
import json
import tempfile
from pathlib import Path

from eimemory.autonomous.exp_log import ExpLog, ExpLogEntry


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
