"""Isolated AST-only ownership probe; no target-project import or real export I/O."""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
from pathlib import Path
from threading import RLock
import unittest


BASELINE_SHA256 = "f0264fe2736c3e05403d5f59716fd8a84f45fb7ba29b282acb8749a22866376a"
METHOD_RANGES = {"flush_exports": (2167, 2209), "_safe_post_commit_projection": (379, 399)}


def extract_shell(source_path: Path, expected_sha256: str):
    data = source_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(source_path))
    owners = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "RuntimeStore"]
    if len(owners) != 1:
        raise ValueError("Expected one RuntimeStore class")
    methods = []
    for name, baseline_range in METHOD_RANGES.items():
        found = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(found) != 1 or found[0].decorator_list:
            raise ValueError(f"Expected exactly one undecorated method: {name}")
        method = found[0]
        if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != baseline_range:
            raise ValueError(f"Baseline method range mismatch: {name}")
        if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(method)):
            raise ValueError(f"Project imports are forbidden inside extracted method: {name}")
        methods.append(copy.deepcopy(method))
        print(f"EXTRACTED {name} L{method.lineno}-{method.end_lineno}")
    shell_node = ast.ClassDef(name="Shell", bases=[], keywords=[], body=methods, decorator_list=[])
    isolated = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), shell_node],
        type_ignores=[],
    )
    ast.fix_missing_locations(isolated)

    def fake_markdown(root, record):
        # Both arguments are opaque test sentinels; no content or file access.
        root.trace.append("markdown")
        if record is not root.record:
            raise AssertionError("Unexpected opaque record")

    namespace = {"export_record_markdown": fake_markdown}
    exec(compile(isolated, str(source_path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace["Shell"]


class FakeSqlite:
    COUNT_SQL = "SELECT COUNT(*) FROM export_outbox WHERE state = 'pending'"

    def __init__(self, trace, rows, *, caller_transaction=False):
        self.trace = trace
        self.rows = rows
        self.conn = self
        self.in_transaction = caller_transaction
        self.caller_pending = caller_transaction
        self.pending_marks = set()
        self.exported = set()

    def pending_exports(self, *, limit, operation_ids):
        self.trace.append("pending")
        return [row for row in self.rows if row["operation_id"] not in self.exported][:limit]

    def mark_exported(self, operation_id, *, commit):
        if commit is not False:
            raise AssertionError("Per-row commit is outside the wrapper contract")
        self.trace.append(f"mark:{operation_id}")
        self.in_transaction = True
        self.pending_marks.add(operation_id)

    def commit(self):
        self.trace.append("commit")
        self.exported.update(self.pending_marks)
        self.pending_marks.clear()
        self.in_transaction = False
        self.caller_pending = False

    def rollback(self):
        self.trace.append("rollback")
        self.pending_marks.clear()
        self.in_transaction = False
        self.caller_pending = False

    def execute(self, sql):
        if sql != self.COUNT_SQL:
            raise AssertionError("Only the wrapper's fake count query is allowed")
        self.trace.append("count")
        return self

    def fetchone(self):
        return (len(self.rows) - len(self.exported),)


class FakeLog:
    def __init__(self, trace, rows, *, failure=None, fail_append_at=None, fail_durable=False):
        self.trace = trace
        self.rows = {row["operation_id"]: row for row in rows}
        self.failure = failure
        self.fail_append_at = fail_append_at
        self.fail_durable = fail_durable
        self.append_calls = 0

    def append_payload(self, payload, *, operation_id, expected_digest, fsync):
        row = self.rows[operation_id]
        if payload is not row["payload"] or expected_digest is not row["payload_digest"] or fsync is not False:
            raise AssertionError("Opaque payload/digest forwarding contract changed")
        self.append_calls += 1
        self.trace.append(f"append:{operation_id}")
        if self.append_calls == self.fail_append_at:
            raise self.failure

    def flush_durable(self):
        self.trace.append("durable")
        if self.fail_durable:
            raise self.failure


def ownership_tests(shell_type):
    class ExportWrapperOwnershipTests(unittest.TestCase):
        def make_shell(self, *, count=2, caller_transaction=False, fail_append_at=None, fail_durable=False):
            shell = shell_type()
            shell._lock = RLock()
            shell.trace = []
            shell.record = object()
            shell.root = shell
            rows = [
                {"stream": "records", "operation_id": f"fake-{index}", "payload": object(), "payload_digest": object()}
                for index in range(count)
            ]
            failure = ValueError("fake export failure")
            shell.sqlite = FakeSqlite(shell.trace, rows, caller_transaction=caller_transaction)
            shell.log = FakeLog(shell.trace, rows, failure=failure, fail_append_at=fail_append_at, fail_durable=fail_durable)
            shell._auxiliary_log = lambda _stream: shell.log
            shell._flush_committed_exports = lambda *ids: shell.flush_exports(operation_ids=list(ids))
            return shell, failure, rows

        def assert_owned_failure_clean(self, shell, failure):
            with self.assertRaises(ValueError) as caught:
                shell.flush_exports()
            self.assertIs(caught.exception, failure)
            self.assertFalse(shell.sqlite.in_transaction)
            self.assertEqual(shell.sqlite.pending_marks, set())
            self.assertEqual(shell.sqlite.exported, set())
            self.assertEqual(shell.trace.count("rollback"), 1)
            self.assertNotIn("commit", shell.trace)

        def test_normal_owned_flush_preserves_durability_order(self):
            shell, _, _ = self.make_shell()
            self.assertEqual(shell.flush_exports(), {"ok": True, "exported": 2, "remaining": 0})
            self.assertEqual(shell.trace, ["pending", "append:fake-0", "mark:fake-0", "append:fake-1", "mark:fake-1", "durable", "commit", "count"])
            self.assertFalse(shell.sqlite.in_transaction)

        def test_later_append_failure_cleans_owned_transaction(self):
            shell, failure, _ = self.make_shell(fail_append_at=2)
            self.assert_owned_failure_clean(shell, failure)
            self.assertEqual(shell.trace, ["pending", "append:fake-0", "mark:fake-0", "append:fake-1", "rollback"])

        def test_durable_flush_failure_cleans_owned_transaction(self):
            shell, failure, _ = self.make_shell(fail_durable=True)
            self.assert_owned_failure_clean(shell, failure)
            self.assertEqual(shell.trace[-2:], ["durable", "rollback"])

        def test_failure_before_first_mark_adds_no_rollback_or_commit(self):
            shell, failure, _ = self.make_shell(fail_append_at=1)
            with self.assertRaises(ValueError) as caught:
                shell.flush_exports()
            self.assertIs(caught.exception, failure)
            self.assertFalse(shell.sqlite.in_transaction)
            self.assertEqual(shell.trace, ["pending", "append:fake-0"])

        def test_safe_projection_suppresses_failure_after_owned_cleanup(self):
            shell, _, rows = self.make_shell(fail_append_at=2)
            shell._safe_post_commit_projection(rows, [shell.record])
            self.assertFalse(shell.sqlite.in_transaction)
            self.assertEqual(shell.sqlite.pending_marks, set())
            self.assertEqual(shell.trace.count("rollback"), 1)
            self.assertNotIn("commit", shell.trace)
            self.assertEqual(shell.trace[-1], "markdown")

        def test_preexisting_caller_transaction_is_not_committed_or_rolled_back(self):
            shell, _, _ = self.make_shell(caller_transaction=True)
            with self.assertRaisesRegex(RuntimeError, "flush_exports_requires_own_transaction"):
                shell.flush_exports()
            self.assertEqual(shell.trace, [])
            self.assertTrue(shell.sqlite.in_transaction)
            self.assertTrue(shell.sqlite.caller_pending)
            self.assertNotIn("commit", shell.trace)
            self.assertNotIn("rollback", shell.trace)

        def test_no_pending_rows_adds_no_export_or_transaction(self):
            shell, _, _ = self.make_shell(count=0)
            self.assertEqual(shell.flush_exports(), {"ok": True, "exported": 0, "remaining": 0})
            self.assertEqual(shell.trace, ["pending", "count"])
            self.assertFalse(shell.sqlite.in_transaction)

    return ExportWrapperOwnershipTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    shell_type = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ownership_tests(shell_type))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
