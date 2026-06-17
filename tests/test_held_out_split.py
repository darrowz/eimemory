"""Tests for deterministic 70/30 held-out split of learning_playbook records.

The 145-record split is the Phase 1 acceptance gate per the karpathy-loop plan
(`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md` Task 1.2):

  - Held-out split: train=101 holdout=44 (145 × 70/30)
  - Same seed -> same split (deterministic, reproducible across reruns)
"""
from __future__ import annotations

import json
from pathlib import Path

from eimemory.governance.held_out_split import (
    HOLD_OUT_PATH,
    RECORDS_PATH,
    TRAIN_PATH,
    split_playbooks,
)


def _write_145_records(records_path: Path) -> None:
    """Write 145 minimal `learning_playbook` records to a jsonl file."""
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("w", encoding="utf-8") as f:
        for i in range(145):
            f.write(
                json.dumps({"kind": "learning_playbook", "record_id": f"r{i:03d}"})
                + "\n"
            )


def test_module_path_defaults_substitute_eimemory_root():
    """Module defaults use E:/eimemory/... (Windows-friendly substitute for /var/lib/eimemory/...)."""
    assert str(RECORDS_PATH).replace("\\", "/").startswith("E:/eimemory")
    assert str(HOLD_OUT_PATH).replace("\\", "/").startswith("E:/eimemory")
    assert str(TRAIN_PATH).replace("\\", "/").startswith("E:/eimemory")


def test_split_creates_hold_out_file(tmp_path: Path):
    records = tmp_path / "records.jsonl"
    hold_out = tmp_path / "held_out_playbooks.json"
    train = tmp_path / "train_playbooks.json"
    _write_145_records(records)

    split_playbooks(
        records_path=records,
        hold_out_path=hold_out,
        train_path=train,
        seed=42,
    )

    assert hold_out.exists()
    assert train.exists()


def test_split_70_30_ratio(tmp_path: Path):
    records = tmp_path / "records.jsonl"
    hold_out = tmp_path / "held_out_playbooks.json"
    train = tmp_path / "train_playbooks.json"
    _write_145_records(records)

    split_playbooks(
        records_path=records,
        hold_out_path=hold_out,
        train_path=train,
        seed=42,
    )

    hold_out_ids = json.loads(hold_out.read_text(encoding="utf-8"))
    train_ids = json.loads(train.read_text(encoding="utf-8"))
    # 30% ±5% on the holdout slice; train + holdout = 145
    assert 0.25 <= len(hold_out_ids) / 145 <= 0.35
    assert len(train_ids) + len(hold_out_ids) == 145
    # Disjoint sets
    assert set(train_ids).isdisjoint(set(hold_out_ids))


def test_split_is_deterministic(tmp_path: Path):
    records = tmp_path / "records.jsonl"
    hold_out = tmp_path / "held_out_playbooks.json"
    train = tmp_path / "train_playbooks.json"
    _write_145_records(records)

    split_playbooks(
        records_path=records,
        hold_out_path=hold_out,
        train_path=train,
        seed=42,
    )
    first = json.loads(hold_out.read_text(encoding="utf-8"))

    split_playbooks(
        records_path=records,
        hold_out_path=hold_out,
        train_path=train,
        seed=42,
    )
    second = json.loads(hold_out.read_text(encoding="utf-8"))

    assert sorted(first) == sorted(second)


def test_split_acceptance_gate_train_101_holdout_44(tmp_path: Path):
    """Phase 1 acceptance gate: 145 × 70/30 must yield train=101 holdout=44."""
    records = tmp_path / "records.jsonl"
    hold_out = tmp_path / "held_out_playbooks.json"
    train = tmp_path / "train_playbooks.json"
    _write_145_records(records)

    train_ids, holdout_ids = split_playbooks(
        records_path=records,
        hold_out_path=hold_out,
        train_path=train,
        seed=42,
    )

    assert len(train_ids) == 101
    assert len(holdout_ids) == 44
