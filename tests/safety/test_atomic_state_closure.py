from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path

import pytest

from eimemory.governance.safety.audit import AuditLog
from eimemory.governance.safety.circuit_breaker import BudgetExceeded, CircuitBreaker
from eimemory.governance.safety.l3_queue import L3Queue


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


def test_circuit_breaker_multi_instance_budget_is_atomic(tmp_path: Path) -> None:
    action = "multi_instance_atomic_budget"

    def consume_once(_: int) -> bool:
        breaker = CircuitBreaker(root=tmp_path, default_budget=1)
        try:
            breaker.consume(action)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume_once, range(8)))

    assert outcomes.count(True) == 1
    assert CircuitBreaker(root=tmp_path, default_budget=1).remaining(action) == 0


def test_l3_approval_preserves_concurrent_new_requests(tmp_path: Path) -> None:
    queue = L3Queue(tmp_path)
    first_id = queue.request(action_class="deploy", payload={"n": 1}, requester="loop")

    # Simulate a second process adding a new request while the first process is
    # approving an older request.
    second = {
        "id": "second",
        "action_class": "send_external_message",
        "payload": {"n": 2},
        "requester": "other-loop",
        "status": "pending_human",
        "created_at": "2026-07-11T00:00:00+00:00",
        "approver": None,
        "approved_at": None,
    }

    original_read_all = queue._read_all
    injected = {"done": False}

    def read_all_with_interleaved_append():
        records = original_read_all()
        if not injected["done"]:
            injected["done"] = True
            with queue.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(second, ensure_ascii=False) + "\n")
        return records

    queue._read_all = read_all_with_interleaved_append  # type: ignore[method-assign]
    queue.approve(first_id, approver="hongtu")

    persisted_ids = {record["id"] for record in L3Queue(tmp_path)._read_all()}
    assert {first_id, "second"}.issubset(persisted_ids)


def test_l3_approving_missing_request_does_not_create_state(tmp_path: Path) -> None:
    queue = L3Queue(tmp_path)
    with pytest.raises(KeyError):
        queue.approve("missing", approver="hongtu")
    assert queue.list_pending() == []
