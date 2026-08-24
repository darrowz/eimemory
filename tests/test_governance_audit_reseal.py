"""Regression tests for the audit log reseal tool (mechanism-level)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eimemory.governance.safety.audit import AuditLog, ChainBroken
from eimemory.governance.safety.reseal_audit_log import plan, reseal


def _write_legacy(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_dry_run_plan_counts_legacy_rows(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    _write_legacy(p, [
        {"event": "emergency_stop", "at": "2026-07-07T16:51:04+00:00", "pid": 1},
        {"event": "emergency_stop", "at": "2026-07-07T16:52:38+00:00", "pid": 2},
    ])
    legacy, notes = plan(p)
    assert len(legacy) == 2
    assert legacy[0]["_lineno"] == 1
    assert any("2 legacy rows" in n for n in notes)


def test_reseal_preserves_history_and_verifies(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    original = [
        {"event": "emergency_stop", "at": "2026-07-07T16:51:04+00:00", "pid": 1},
        {"event": "audit_chain_broken", "ts": "2026-08-24T04:00:17+00:00", "row_index": 0},
    ]
    _write_legacy(p, original)

    backup = reseal(p, operator="test")

    # Live file now verifies clean — the whole point.
    AuditLog(p).verify()  # must not raise

    # History preserved verbatim inside chained rows (flat format).
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert rows[0]["event"] == "reseal_baseline"
    imported = [r for r in rows if r.get("event") == "legacy_import"]
    assert len(imported) == 2
    assert imported[0]["payload"] == original[0]
    assert imported[0]["original_line_number"] == 1

    # Original file kept, never deleted.
    assert backup.exists()
    kept = [json.loads(l) for l in backup.read_text().splitlines()]
    assert kept == original

    # New appends continue the chain cleanly.
    AuditLog(p).append({"event": "post_reseal_event"})
    AuditLog(p).verify()


def test_reseal_refuses_mixed_log(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    _write_legacy(p, [{"event": "old", "at": "2026-01-01T00:00:00+00:00"}])
    AuditLog(p).append({"event": "new_chained_row"})  # mixes formats
    with pytest.raises(SystemExit, match="mixes"):
        reseal(p, operator="test")


def test_reseal_idempotent_on_chained_log(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append({"event": "already_chained"})
    with pytest.raises(SystemExit, match="already fully chained"):
        reseal(p, operator="test")


def test_corrupt_json_aborts(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text('{"event": "ok"}\nnot-json-at-all\n')
    with pytest.raises(SystemExit, match="not valid JSON"):
        plan(p)
