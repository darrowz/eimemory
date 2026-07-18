from __future__ import annotations

import json
from pathlib import Path

from eimemory.ops import openclaw_loop


def _write_raw_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def test_read_jsonl_incrementally_parses_only_appended_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    openclaw_loop.reset_jsonl_cache_for_tests()
    path = tmp_path / "tasks.jsonl"
    _write_raw_jsonl(path, [{"task_id": "task-1"}, {"task_id": "task-2"}])

    loads_calls = 0
    original_loads = openclaw_loop.json.loads

    def counting_loads(value: str):
        nonlocal loads_calls
        loads_calls += 1
        return original_loads(value)

    monkeypatch.setattr(openclaw_loop.json, "loads", counting_loads)

    assert [row["task_id"] for row in openclaw_loop.read_jsonl("tasks.jsonl")] == ["task-1", "task-2"]
    assert loads_calls == 2

    loads_calls = 0
    _write_raw_jsonl(path, [{"task_id": "task-3"}])

    assert [row["task_id"] for row in openclaw_loop.read_jsonl("tasks.jsonl")] == [
        "task-1",
        "task-2",
        "task-3",
    ]
    assert loads_calls == 1


def test_append_jsonl_updates_existing_cache_without_reparse(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    openclaw_loop.reset_jsonl_cache_for_tests()
    _write_raw_jsonl(tmp_path / "tasks.jsonl", [{"task_id": "task-1"}])
    assert len(openclaw_loop.read_jsonl("tasks.jsonl")) == 1

    def fail_loads(_value: str):
        raise AssertionError("cached append should not reparse existing JSONL")

    monkeypatch.setattr(openclaw_loop.json, "loads", fail_loads)

    openclaw_loop.append_jsonl("tasks.jsonl", {"task_id": "task-2"})

    rows = openclaw_loop.read_jsonl("tasks.jsonl")
    assert [row["task_id"] for row in rows] == ["task-1", "task-2"]


def test_append_lock_uses_non_append_binary_lock_file_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    openclaw_loop.reset_jsonl_cache_for_tests()
    lock_modes: list[str] = []
    original_open = Path.open

    def recording_open(self: Path, mode: str = "r", *args, **kwargs):
        if self.name.endswith(".lock"):
            lock_modes.append(mode)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    openclaw_loop.append_jsonl("tasks.jsonl", {"task_id": "task-lock-mode"})

    assert "a+b" not in lock_modes
    assert "r+b" in lock_modes


def test_large_ledger_is_not_retained_in_process_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    monkeypatch.setattr(openclaw_loop, "MAX_JSONL_CACHE_ROWS", 2)
    openclaw_loop.reset_jsonl_cache_for_tests()
    _write_raw_jsonl(
        tmp_path / "tasks.jsonl",
        [{"task_id": f"task-{index}"} for index in range(3)],
    )

    assert len(openclaw_loop.read_jsonl("tasks.jsonl")) == 3
    assert openclaw_loop._JSONL_CACHE == {}
