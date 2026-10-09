"""Offline selected migration-loop regression, without DB or maintenance IO."""

from pathlib import Path
from types import SimpleNamespace as NS
import unittest


def live_runner(namespace):
    path = Path(__file__).resolve().parents[1] / "eimemory/storage/maintenance.py"
    lines = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("def _run_storage_migrations_locked("):
                lines.append(line)
                break
        else:
            raise AssertionError("missing selected runner")
        for line in stream:
            if line.startswith("def "):
                break
            lines.append(line)
    exec(compile("from __future__ import annotations\n" + "".join(lines),
                 "<isolated migration loop>", "exec"), namespace)
    return namespace["_run_storage_migrations_locked"]


class InertStore:
    def __init__(self, remaining, fail=False):
        self.remaining = remaining
        self.fail = fail
        self.calls = []
        self.closed = 0

    def pending_storage_migrations(self):
        return ["inert-migration"] if self.remaining else []

    def apply_storage_migrations(self, *, batch_size, offline):
        self.calls.append((batch_size, offline))
        if self.fail:
            raise RuntimeError("inert failure")
        self.remaining -= 1
        return {"processed": 5}

    def close(self):
        self.closed += 1


class BatchBoundaryTests(unittest.TestCase):
    def run_fixture(self, remaining, max_batches, ticks=(0, 0, 0), fail=False, max_seconds=10):
        store = InertStore(remaining, fail=fail)
        clock = iter(ticks)
        runner = live_runner({"SqliteRecordStore": lambda path: store,
                              "time": NS(monotonic=lambda: next(clock))})
        return store, lambda: runner(db_path="inert-path", offline=True, batch_size=7,
                                    max_batches=max_batches, max_seconds=max_seconds)

    def test_finishing_on_last_allowed_batch_succeeds(self):
        for budget in (1, 2):
            with self.subTest(budget=budget):
                store, run = self.run_fixture(budget, budget)
                report = run()
                self.assertTrue(report["ok"])
                self.assertEqual(report["pending"], [])
                self.assertNotIn("reason", report)
                self.assertEqual((report["batch_count"], report["processed"]), (budget, budget * 5))
                self.assertEqual(store.calls, [(7, True)] * budget)
                self.assertEqual(store.closed, 1)

    def test_remaining_work_still_reports_batch_limit(self):
        store, run = self.run_fixture(2, 1)
        report = run()
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "max_batches_exceeded")
        self.assertEqual(report["pending"], ["inert-migration"])
        self.assertEqual((report["batch_count"], report["processed"], store.closed), (1, 5, 1))

    def test_zero_budget_with_no_pending_work_succeeds(self):
        store, run = self.run_fixture(0, 0)
        report = run()
        self.assertTrue(report["ok"])
        self.assertNotIn("reason", report)
        self.assertEqual((report["batch_count"], report["processed"], store.calls, store.closed), (0, 0, [], 1))

    def test_already_completed_work_does_not_apply_a_batch(self):
        store, run = self.run_fixture(0, 1)
        self.assertTrue(run()["ok"])
        self.assertEqual((store.calls, store.closed), ([], 1))

    def test_time_limit_retains_failure_and_closes_store(self):
        store, run = self.run_fixture(1, 1, ticks=(0, 2), max_seconds=1)
        report = run()
        self.assertEqual((report["ok"], report["reason"]), (False, "max_seconds_exceeded"))
        self.assertEqual((store.calls, store.closed), ([], 1))

    def test_batch_exception_still_closes_store(self):
        store, run = self.run_fixture(1, 1, fail=True)
        with self.assertRaisesRegex(RuntimeError, "inert failure"):
            run()
        self.assertEqual(store.closed, 1)


if __name__ == "__main__":
    unittest.main()
