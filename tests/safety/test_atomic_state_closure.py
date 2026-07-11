from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from eimemory.governance.safety.audit import AuditLog
from eimemory.governance.safety.circuit_breaker import BudgetExceeded, CircuitBreaker
from eimemory.governance.safety.l3_queue import L3Queue


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
