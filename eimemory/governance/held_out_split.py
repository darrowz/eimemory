"""Deterministic 70/30 held-out split of `learning_playbook` records.

Reads every `learning_playbook` record id from a JSONL records file, then
performs a seeded shuffle and a 70/30 cut:

    train = ids[:cut]      # 70% (101/145)
    holdout = ids[cut:]    # 30% ( 44/145)

The shuffle seed is part of the public API so reruns with the same seed
yield byte-identical splits. Default paths use the Windows-friendly
`E:/eimemory/...` substitute for the Linux `/var/lib/eimemory/...` paths
called out in the Phase 1 plan; tests can override via keyword args.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Iterator

# Default paths (Windows-friendly substitute for /var/lib/eimemory/...).
DEFAULT_RECORDS_PATH = Path("E:/eimemory/records.jsonl")
DEFAULT_HOLD_OUT_PATH = Path(
    "E:/eimemory/state/autonomous_learning/held_out_playbooks.json"
)
DEFAULT_TRAIN_PATH = Path(
    "E:/eimemory/state/autonomous_learning/train_playbooks.json"
)

# Backwards-compatible module-level constants for any external consumers
# that read the path directly (e.g. the 145-record gate tests).
RECORDS_PATH = DEFAULT_RECORDS_PATH
HOLD_OUT_PATH = DEFAULT_HOLD_OUT_PATH
TRAIN_PATH = DEFAULT_TRAIN_PATH

HOLDOUT_RATIO = 0.30


def _iter_playbooks(records_path: Path) -> Iterator[str]:
    """Yield record_id for every `learning_playbook` row in records_path."""
    if not records_path.exists():
        return
    with records_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "learning_playbook" and "record_id" in row:
                yield str(row["record_id"])


def split_playbooks(
    *,
    seed: int = 42,
    records_path: Path | None = None,
    hold_out_path: Path | None = None,
    train_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Deterministic 70/30 split. Returns (train_ids, holdout_ids).

    Args:
        seed: RNG seed. Same seed + same input -> same split.
        records_path: JSONL records file. Defaults to RECORDS_PATH.
        hold_out_path: Output file for sorted hold-out ids. Defaults to HOLD_OUT_PATH.
        train_path: Output file for sorted train ids. Defaults to TRAIN_PATH.

    Returns:
        (train_ids, holdout_ids) — both sorted lists of record_id strings.
    """
    rec_path = Path(records_path) if records_path is not None else RECORDS_PATH
    hold_path = Path(hold_out_path) if hold_out_path is not None else HOLD_OUT_PATH
    trn_path = Path(train_path) if train_path is not None else TRAIN_PATH

    ids = sorted(_iter_playbooks(rec_path))
    rng = random.Random(seed)
    rng.shuffle(ids)
    cut = int(len(ids) * (1 - HOLDOUT_RATIO))
    train, holdout = ids[:cut], ids[cut:]

    hold_path.parent.mkdir(parents=True, exist_ok=True)
    trn_path.parent.mkdir(parents=True, exist_ok=True)
    hold_path.write_text(json.dumps(sorted(holdout), indent=2), encoding="utf-8")
    trn_path.write_text(json.dumps(sorted(train), indent=2), encoding="utf-8")
    return train, holdout


if __name__ == "__main__":
    train, holdout = split_playbooks()
    print(f"train={len(train)} holdout={len(holdout)}")
