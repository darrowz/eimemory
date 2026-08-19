from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path

import pytest

from eimemory.governance.safety.audit import AuditLog


def _increment_json_state_worker(path_value: str, count: int) -> None:
    from eimemory.storage.atomic_file import locked_json_update

    for _ in range(count):
        locked_json_update(
            Path(path_value),
            lambda current: {"value": current["value"] + 1},
            default={"value": 0},
            expected_type=dict,
        )


def test_locked_json_update_serializes_concurrent_read_modify_write(tmp_path: Path) -> None:
    from eimemory.storage.atomic_file import locked_json_update, read_json_strict

    path = tmp_path / "counter.json"

    def increment(_: int) -> None:
        def mutate(current: dict[str, int]) -> dict[str, int]:
            return {"value": current["value"] + 1}

        locked_json_update(path, mutate, default={"value": 0}, expected_type=dict)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(increment, range(48)))

    assert read_json_strict(path, dict) == {"value": 48}


def test_read_json_strict_rejects_malformed_and_wrong_shape(tmp_path: Path) -> None:
    from eimemory.storage.atomic_file import read_json_strict

    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        read_json_strict(path, dict)

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected object"):
        read_json_strict(path, dict)


def test_locked_json_update_serializes_multiple_processes(tmp_path: Path) -> None:
    from concurrent.futures import ProcessPoolExecutor
    from eimemory.storage.atomic_file import read_json_strict

    path = tmp_path / "process-counter.json"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        futures = [pool.submit(_increment_json_state_worker, str(path), 10) for _ in range(4)]
        for future in futures:
            future.result(timeout=30)

    assert read_json_strict(path, dict) == {"value": 40}


def test_locked_json_update_leaves_previous_state_on_mutation_failure(tmp_path: Path) -> None:
    from eimemory.storage.atomic_file import atomic_write_json, locked_json_update, read_json_strict

    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 7})

    def fail(_current: dict[str, int]) -> dict[str, int]:
        raise RuntimeError("injected mutation failure")

    with pytest.raises(RuntimeError, match="injected"):
        locked_json_update(path, fail, expected_type=dict)

    assert read_json_strict(path, dict) == {"value": 7}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_audit_log_concurrent_appends_keep_single_valid_chain(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"

    def append_one(index: int) -> int:
        row = AuditLog(log_path).append({"actor": "thread", "n": index})
        return row.row_index

    with ThreadPoolExecutor(max_workers=12) as pool:
        indexes = list(pool.map(append_one, range(36)))

    rows = AuditLog(log_path).read_all()
    assert len(rows) == 36
    assert sorted(indexes) == list(range(36))
    assert [row.row_index for row in rows] == list(range(36))
    AuditLog(log_path).verify()
