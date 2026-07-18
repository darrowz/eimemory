from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eimemory.autonomous.business_feedback import compute_business_impact
from eimemory.autonomous.capability_discovery import discover_new_capabilities
from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses
from eimemory.governance.held_out_split import split_playbooks


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_all_record_consumers_stream_archived_and_active_segments(tmp_path: Path) -> None:
    occurred_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    records = tmp_path / "records.jsonl"
    archived = tmp_path / "records.00000001.jsonl"
    archived_rows = [
        {
            "kind": "weakness",
            "record_id": f"w{i}",
            "content": {"summary": "recall timeout"},
            "time": {"occurred_at": occurred_at},
        }
        for i in range(3)
    ]
    archived_rows.extend(
        {
            "kind": "recall_view",
            "record_id": f"rv{i}",
            "content": {"hit_at_1": score},
            "time": {"occurred_at": occurred_at},
        }
        for i, score in enumerate((0.7, 0.8))
    )
    archived_rows.extend(
        {"kind": "learning_playbook", "record_id": f"p{i}"}
        for i in range(3)
    )
    _write_rows(archived, archived_rows)
    _write_rows(
        records,
        [
            {
                "kind": "recall_view",
                "record_id": "rv2",
                "content": {"hit_at_1": 0.9},
                "time": {"occurred_at": occurred_at},
            },
            {"kind": "learning_playbook", "record_id": "p3"},
        ],
    )

    assert "memory.recall_quality" in discover_new_capabilities(
        records_path=records,
        min_count=3,
    )
    impact = compute_business_impact(records_path=records, days=7)
    assert impact["n_recall_view"] == 3
    assert impact["avg_hit_at_1"] == 0.8
    hypotheses = generate_hypotheses_from_weaknesses(records_path=records)
    assert any("appears 3 times" in item for item in hypotheses)

    train, holdout = split_playbooks(
        records_path=records,
        train_path=tmp_path / "train.json",
        hold_out_path=tmp_path / "holdout.json",
    )
    assert sorted([*train, *holdout]) == ["p0", "p1", "p2", "p3"]
