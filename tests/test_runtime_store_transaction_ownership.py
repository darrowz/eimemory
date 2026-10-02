"""Transaction ownership unit tests using a fake, never a SQLite connection."""
from __future__ import annotations

import sqlite3
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eimemory.storage.runtime_store import RuntimeStore


class _FakeTransactionStore:
    def __init__(self, *, in_transaction: bool = False) -> None:
        self.in_transaction = in_transaction
        self.pending = ["caller-owned write"] if in_transaction else []
        self.calls: list[str] = []

    def execute(self, sql: str) -> None:
        self.calls.append(sql)
        if sql != "BEGIN IMMEDIATE":
            raise AssertionError(f"Unexpected SQL in fake: {sql}")
        if self.in_transaction:
            raise sqlite3.OperationalError("cannot start a transaction within a transaction")
        self.in_transaction = True

    def commit(self) -> None:
        self.calls.append("commit")
        self.in_transaction = False
        self.pending.clear()

    def rollback(self) -> None:
        self.calls.append("rollback")
        self.in_transaction = False
        self.pending.clear()

    def upsert_memory_edges(self, edges, *, commit: bool) -> None:
        if edges != [] or commit is not False:
            raise AssertionError("Fake expected empty uncommitted edge write")
        self.calls.append("edges")


class AtomicMutationOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = object.__new__(RuntimeStore)
        self.store._lock = RLock()
        self.store.sqlite = _FakeTransactionStore()
        self.store._safe_post_commit_projection = lambda *_args: None
        self.store._flush_committed_exports = lambda *_args: {"ok": True, "remaining": 0}
        repository = patch(
            "eimemory.storage.runtime_store._open_capability_store",
            return_value=SimpleNamespace(pending_audits=[]),
        )
        repository.start()
        self.addCleanup(repository.stop)

    def _preserves_caller_owned_transaction(self, domain: str) -> None:
        self.store.sqlite = _FakeTransactionStore(in_transaction=True)
        callback_calls = []
        error = None

        try:
            getattr(self.store, f"mutate_{domain}_atomically")(
                lambda _repository: callback_calls.append("called")
            )
        except Exception as exc:
            error = exc

        self.assertTrue(self.store.sqlite.in_transaction)
        self.assertEqual(self.store.sqlite.pending, ["caller-owned write"])
        self.assertEqual(self.store.sqlite.calls, [])
        self.assertEqual(callback_calls, [])
        self.assertIsInstance(error, RuntimeError)
        expected = "record" if domain == "records" else "capability"
        self.assertEqual(str(error), f"{expected}_mutation_requires_own_transaction")

    def test_records_preserve_caller_owned_transaction(self) -> None:
        self._preserves_caller_owned_transaction("records")

    def test_capabilities_preserve_caller_owned_transaction(self) -> None:
        self._preserves_caller_owned_transaction("capabilities")

    def _commits_own_transaction(self, domain: str) -> None:
        callback_calls = []

        def mutation(_repository):
            self.assertTrue(self.store.sqlite.in_transaction)
            callback_calls.append("called")
            return ("result", [], []) if domain == "records" else "result"

        self.assertEqual(getattr(self.store, f"mutate_{domain}_atomically")(mutation), "result")
        self.assertEqual(callback_calls, ["called"])
        self.assertFalse(self.store.sqlite.in_transaction)
        expected = ["BEGIN IMMEDIATE"]
        if domain == "records":
            expected.append("edges")
        self.assertEqual(self.store.sqlite.calls, [*expected, "commit"])

    def test_records_commit_own_transaction(self) -> None:
        self._commits_own_transaction("records")

    def test_capabilities_commit_own_transaction(self) -> None:
        self._commits_own_transaction("capabilities")

    def _rolls_back_own_failed_transaction(self, domain: str) -> None:
        failure = ValueError("fake callback failure")

        def mutation(_repository):
            self.assertTrue(self.store.sqlite.in_transaction)
            self.store.sqlite.pending.append("failed callback write")
            raise failure

        with self.assertRaises(ValueError) as raised:
            getattr(self.store, f"mutate_{domain}_atomically")(mutation)

        self.assertIs(raised.exception, failure)
        self.assertFalse(self.store.sqlite.in_transaction)
        self.assertEqual(self.store.sqlite.pending, [])
        self.assertEqual(self.store.sqlite.calls, ["BEGIN IMMEDIATE", "rollback"])

    def test_records_roll_back_own_failed_transaction(self) -> None:
        self._rolls_back_own_failed_transaction("records")

    def test_capabilities_roll_back_own_failed_transaction(self) -> None:
        self._rolls_back_own_failed_transaction("capabilities")


    def _rolls_back_own_aborted_transaction(self, domain: str) -> None:
        class FakeCallbackAbort(BaseException):
            pass

        failure = FakeCallbackAbort("fake callback abort; no process signal")
        projection_calls = []
        self.store._safe_post_commit_projection = lambda *_args: projection_calls.append("records")
        self.store._flush_committed_exports = lambda *_args: projection_calls.append("capabilities")
        callback_calls = []

        def mutation(_repository):
            self.assertTrue(self.store.sqlite.in_transaction)
            callback_calls.append("called")
            self.store.sqlite.pending.append("aborted callback write")
            raise failure

        with self.assertRaises(FakeCallbackAbort) as raised:
            getattr(self.store, f"mutate_{domain}_atomically")(mutation)

        self.assertIs(raised.exception, failure)
        self.assertEqual(callback_calls, ["called"])
        self.assertFalse(self.store.sqlite.in_transaction)
        self.assertEqual(self.store.sqlite.pending, [])
        self.assertEqual(self.store.sqlite.calls, ["BEGIN IMMEDIATE", "rollback"])
        self.assertEqual(projection_calls, [])

    def test_records_roll_back_own_aborted_transaction(self) -> None:
        self._rolls_back_own_aborted_transaction("records")

    def test_capabilities_roll_back_own_aborted_transaction(self) -> None:
        self._rolls_back_own_aborted_transaction("capabilities")


if __name__ == "__main__":
    unittest.main()
