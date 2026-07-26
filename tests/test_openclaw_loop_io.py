from __future__ import annotations

import hashlib
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


def test_watch_creates_repair_task_reconciles_old_stale_work_and_rechecks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = openclaw_loop.create_task(title="old", objective="old", source="test")
    openclaw_loop.update_task(stale["task_id"], lease_expires_at=1, updated_at="2026-01-01T00:00:00Z")

    result = openclaw_loop.run_watch(
        run_live_checks=False,
        auto_reconcile=True,
        reconcile_grace_seconds=0,
    )

    assert result["ok"] is True
    assert result["repair"]["created"] is True
    assert result["repair"]["reconciled_count"] == 1
    assert result["repair"]["remaining_stale_count"] == 0
    assert openclaw_loop.get_task(stale["task_id"])["status"] == "failed"


def test_watch_leaves_stale_task_inside_reconcile_grace_period_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = openclaw_loop.create_task(title="recent", objective="recent", source="test")
    openclaw_loop.update_task(stale["task_id"], lease_expires_at=openclaw_loop.now_epoch() - 1)

    result = openclaw_loop.run_watch(run_live_checks=False)

    assert result["ok"] is False
    assert result["repair"]["created"] is False
    assert result["repair"]["eligible_count"] == 0
    assert result["repair"]["remaining_stale_count"] == 1
    assert openclaw_loop.get_task(stale["task_id"])["status"] == "planned"


def test_watch_repair_is_idempotent_and_records_only_bounded_audit_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = openclaw_loop.create_task(title="old", objective="old", source="test")
    openclaw_loop.update_task(stale["task_id"], lease_expires_at=1)

    first = openclaw_loop.run_watch(run_live_checks=False, reconcile_grace_seconds=0)
    second = openclaw_loop.run_watch(run_live_checks=False, reconcile_grace_seconds=0)

    repair_tasks = [task for task in openclaw_loop.load_tasks() if task["source"] == "openclaw.loop_watch.repair"]
    repair_verification = openclaw_loop.read_jsonl("verifications.jsonl")[-1]

    assert first["repair"]["created"] is True
    assert second["repair"]["created"] is False
    assert len(repair_tasks) == 1
    assert repair_tasks[0]["status"] == "done"
    assert stale["task_id"] not in json.dumps(repair_verification["checks"])
    assert repair_verification["checks"]["reconciled_count"] == 1
    assert repair_verification["checks"]["remaining"]["count"] == 0


def test_watch_resumes_interrupted_repair_without_reconciling_the_repair_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = openclaw_loop.create_task(title="old", objective="old", source="test")
    openclaw_loop.update_task(stale["task_id"], lease_expires_at=1)
    dedupe = "loop-watch-repair:" + hashlib.sha256(stale["task_id"].encode("utf-8")).hexdigest()[:16]
    interrupted = openclaw_loop.create_task(
        title="OpenClaw loop watchdog stale-task repair",
        objective="reconcile stale OpenClaw loop work and verify the remaining count",
        source="openclaw.loop_watch.repair",
        dedupe_key=dedupe,
    )
    openclaw_loop.update_task(interrupted["task_id"], lease_expires_at=1)

    result = openclaw_loop.run_watch(run_live_checks=False, reconcile_grace_seconds=0)

    repair_tasks = [task for task in openclaw_loop.load_tasks() if task["source"] == "openclaw.loop_watch.repair"]
    repair_verification = openclaw_loop.read_jsonl("verifications.jsonl")[-1]
    assert result["ok"] is True
    assert result["repair"]["created"] is False
    assert result["repair"]["reconciled_count"] == 1
    assert openclaw_loop.get_task(stale["task_id"])["status"] == "failed"
    assert openclaw_loop.get_task(interrupted["task_id"])["status"] == "done"
    assert len(repair_tasks) == 1
    assert stale["task_id"] not in json.dumps(repair_verification["checks"])
    assert interrupted["task_id"] not in json.dumps(repair_verification["checks"])


def test_watch_closes_interrupted_repair_after_its_target_is_already_reconciled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = openclaw_loop.create_task(title="old", objective="old", source="test")
    openclaw_loop.update_task(stale["task_id"], lease_expires_at=1)
    openclaw_loop.reconcile_stale_tasks(apply=True)
    interrupted = openclaw_loop.create_task(
        title="OpenClaw loop watchdog stale-task repair",
        objective="reconcile stale OpenClaw loop work and verify the remaining count",
        source="openclaw.loop_watch.repair",
        dedupe_key="interrupted-repair",
    )
    openclaw_loop.update_task(interrupted["task_id"], lease_expires_at=1)

    result = openclaw_loop.run_watch(run_live_checks=False, reconcile_grace_seconds=0)

    assert result["ok"] is True
    assert result["repair"]["created"] is False
    assert result["repair"]["reconciled_count"] == 0
    assert openclaw_loop.get_task(interrupted["task_id"])["status"] == "done"


def test_watch_closes_each_interrupted_repair_without_counting_other_repair_controls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    repairs = [
        openclaw_loop.create_task(
            title="OpenClaw loop watchdog stale-task repair",
            objective="reconcile stale OpenClaw loop work and verify the remaining count",
            source="openclaw.loop_watch.repair",
            dedupe_key=f"interrupted-repair-{index}",
        )
        for index in range(2)
    ]
    for repair in repairs:
        openclaw_loop.update_task(repair["task_id"], lease_expires_at=1)

    result = openclaw_loop.run_watch(run_live_checks=False, reconcile_grace_seconds=0)

    verifications = [
        verification
        for verification in openclaw_loop.read_jsonl("verifications.jsonl")
        if verification["verifier"] == "openclaw_loop_watch_repair"
    ]
    assert result["ok"] is True
    assert all(openclaw_loop.get_task(repair["task_id"])["status"] == "done" for repair in repairs)
    assert len(verifications) == 2
    assert all(verification["passed"] is True for verification in verifications)
