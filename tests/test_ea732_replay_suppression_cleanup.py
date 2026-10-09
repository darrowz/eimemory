"""Pure fake-target execution of one live method; no DB or file replay."""
from pathlib import Path
from types import SimpleNamespace, MethodType
import re
import textwrap
import unittest


def replay_method(scan_error=False):
    path = Path(__file__).parents[1] / "eimemory/storage/runtime_store.py"
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("    def _replay_jsonl_into("))
    end = start + 1
    while end < len(lines) and not re.match(r"    (?:def |@)", lines[end]):
        end += 1
    namespace = {"_validate_rebuild_counts": lambda _: None}
    exec(compile("from __future__ import annotations\n" + textwrap.dedent("".join(lines[start:end])),
                 str(path), "exec"), namespace)

    def scan():
        if scan_error:
            raise RuntimeError("scan failed")
        return []

    empty_log = SimpleNamespace(scan_strict=lambda: [])
    fixture = SimpleNamespace(log=SimpleNamespace(scan_strict=scan), _auxiliary_log=lambda _: empty_log)
    return MethodType(namespace["_replay_jsonl_into"], fixture)


class FakeTarget:
    def __init__(self, previous=False, fail_at=None, rollback_error=False, commit_error=False):
        self.suppress_auxiliary_logging = previous
        self.fail_at = fail_at
        self.rollback_error = rollback_error
        self.commit_error = commit_error
        self.executions = []
        self.rollbacks = 0
        self.commits = 0

    def execute(self, statement):
        self.executions.append((statement, self.suppress_auxiliary_logging))
        if len(self.executions) == self.fail_at:
            raise RuntimeError("setup failed")

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error:
            raise RuntimeError("rollback failed")

    def commit(self):
        self.commits += 1
        if self.commit_error:
            raise RuntimeError("commit failed")


class ReplayCleanupTest(unittest.TestCase):
    def test_either_setup_failure_restores_both_initial_flags(self):
        for previous in (False, True):
            for fail_at in (1, 2):
                with self.subTest(previous=previous, fail_at=fail_at):
                    target = FakeTarget(previous, fail_at)
                    with self.assertRaisesRegex(RuntimeError, "setup failed"):
                        replay_method()(target)
                    self.assertEqual(target.suppress_auxiliary_logging, previous)
                    self.assertEqual(target.rollbacks, 1)
                    self.assertEqual(target.commits, 0)

    def test_rollback_failure_still_restores_flag(self):
        target = FakeTarget(fail_at=2, rollback_error=True)
        with self.assertRaisesRegex(RuntimeError, "rollback failed"):
            replay_method()(target)
        self.assertFalse(target.suppress_auxiliary_logging)

    def test_empty_replay_commits_and_restores_both_initial_flags(self):
        for previous in (False, True):
            with self.subTest(previous=previous):
                target = FakeTarget(previous)
                counts = replay_method()(target)
                self.assertTrue(all(count == 0 for count in counts.values()))
                self.assertEqual(len(counts), 7)
                self.assertEqual(target.suppress_auxiliary_logging, previous)
                self.assertTrue(all(flag for _, flag in target.executions))
                self.assertEqual(target.commits, 1)
                self.assertEqual(target.rollbacks, 0)

    def test_existing_scan_failure_rolls_back_and_restores(self):
        target = FakeTarget()
        with self.assertRaisesRegex(RuntimeError, "scan failed"):
            replay_method(scan_error=True)(target)
        self.assertFalse(target.suppress_auxiliary_logging)
        self.assertEqual(target.rollbacks, 1)

    def test_existing_commit_failure_rolls_back_and_restores(self):
        target = FakeTarget(commit_error=True)
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            replay_method()(target)
        self.assertFalse(target.suppress_auxiliary_logging)
        self.assertEqual(target.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
