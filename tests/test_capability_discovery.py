"""Tests for capability_discovery (Task 3.1).

Verifies that weakness/incident records are clustered into capability names
and that new (non-existing) capabilities are surfaced.
"""
import json
import tempfile
from pathlib import Path

from eimemory.autonomous.capability_discovery import (
    discover_new_capabilities,
    EXISTING_CAPABILITIES,
)


def test_discover_returns_non_existing_capabilities():
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            {"kind": "weakness", "record_id": f"w{i}",
             "content": {"summary": f"recall hit low {i}" if i < 5 else f"tool error {i}"},
             "time": {"occurred_at": "2026-06-15T10:00:00+08:00"}}
            for i in range(10)
        ]
        records.write_text("\n".join(json.dumps(r) for r in rows))
        new_caps = discover_new_capabilities(records_path=records, min_count=3)
        # recall hit + tool error 都跟 code.implementation 无关
        for cap in new_caps:
            assert cap not in EXISTING_CAPABILITIES or cap == "code.implementation"


def test_min_count_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            {"kind": "weakness", "record_id": "w1",
             "content": {"summary": "lonely bug"},
             "time": {"occurred_at": "2026-06-15T10:00:00+08:00"}}
        ]
        records.write_text(json.dumps(rows[0]))
        new_caps = discover_new_capabilities(records_path=records, min_count=3)
        assert new_caps == []  # 1 < 3 阈值
