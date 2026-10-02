"""AST-only enqueue normalization with inert lock/load/save/UUID/clock substitutes."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "2ef3143bed688cc220246bbb4a1f08560819273937fe8afc085db4c35c1198d6"
GENERATED = "0123456789abcdef"


class FakeHex:
    hex = "0123456789abcdef0123456789abcdef"
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected fake UUID field {name}")


class FakeLock:
    def __init__(self):
        self.enters = 0; self.exits = []; self.depth = 0
    def __enter__(self):
        self.enters += 1; self.depth += 1
        return self
    def __exit__(self, error_type, error, traceback):
        self.depth -= 1; self.exits.append((error_type, error))
        return False


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "L1ExtractQueue"]
    if len(owners) != 1:
        raise ValueError("Expected one queue class")
    found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == "enqueue"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated enqueue method")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (44, 67):
        raise ValueError("Unexpected baseline method boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/storage/helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[])], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED enqueue L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def job_tests(namespace, forbidden):
    class QueueJobIdTests(unittest.TestCase):
        def setUp(self):
            self.queue = namespace["Shell"](); self.lock = FakeLock(); self.lock_marker = object()
            self.queue.lock_path = self.lock_marker
            self.old_jobs = [object(), object()]
            self.dead = object(); self.extra = object(); self.meta = object()
            self.payload = {"jobs": self.old_jobs, "dead": self.dead, "opaque_extra": self.extra}
            self.load_calls = 0; self.save_calls = []; self.save_failure = None
            self.uuid_calls = 0; self.clock_calls = 0; self.lock_calls = []
            def fake_lock(marker):
                self.assertIs(marker, self.lock_marker)
                self.lock_calls.append(marker)
                return self.lock
            def load():
                self.assertEqual(self.lock.depth, 1)
                self.load_calls += 1
                return self.payload
            def save(payload):
                self.assertEqual(self.lock.depth, 1)
                self.save_calls.append(payload)
                if self.save_failure is not None:
                    raise self.save_failure
            def fake_uuid():
                self.uuid_calls += 1
                return FakeHex()
            def fake_now():
                self.clock_calls += 1
                return "synthetic default time"
            self.queue._load = load; self.queue._save = save
            namespace.update(interprocess_lock=fake_lock, uuid4=fake_uuid, _now=fake_now)
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(self.lock.depth, 0)
        def enqueue(self, job):
            self.assertTrue("episode_id" not in job or job["episode_id"] == "")
            self.assertTrue(set(job) <= {"job_id", "episode_id", "created_at", "opaque_metadata"})
            return self.queue.enqueue(job)
        def assert_id(self, record, expected):
            self.assertIs(type(record["job_id"]), str)
            self.assertEqual(record["job_id"], expected)
        def test_missing_id_keeps_generated_string(self):
            job = {}
            record = self.enqueue(job)
            self.assertEqual(job, {})
            self.assert_id(record, GENERATED)
            self.assertEqual(self.uuid_calls, 1)
        def test_false_like_scalar_ids_keep_generated_string(self):
            for value in (None, "", 0, False):
                with self.subTest(value=value):
                    before = self.uuid_calls
                    job = {"job_id": value}; original = dict(job)
                    record = self.enqueue(job)
                    self.assertEqual(job, original)
                    self.assertIs(job["job_id"], value)
                    self.assert_id(record, GENERATED)
                    self.assertEqual(self.uuid_calls, before + 1)
        def test_nonempty_string_keeps_existing_value_without_uuid(self):
            record = self.enqueue({"job_id": "  ordinary-supplied-id  "})
            self.assert_id(record, "  ordinary-supplied-id  ")
            self.assertEqual(self.uuid_calls, 0)
        def test_positive_integer_keeps_existing_decimal_conversion(self):
            job = {"job_id": 42}
            record = self.enqueue(job)
            self.assertEqual(job, {"job_id": 42})
            self.assertIs(type(job["job_id"]), int)
            self.assert_id(record, "42")
            self.assertEqual(self.uuid_calls, 0)
        def test_return_save_order_and_opaque_values_are_preserved(self):
            job = {"job_id": "ordinary-id", "opaque_metadata": self.meta}
            before = dict(job)
            record = self.enqueue(job)
            self.assertEqual(job, before)
            self.assertIs(job["opaque_metadata"], self.meta)
            self.assertIs(record["opaque_metadata"], self.meta)
            self.assertEqual(len(self.save_calls), 1)
            self.assertIs(self.save_calls[0], self.payload)
            saved_jobs = self.save_calls[0]["jobs"]
            self.assertIs(saved_jobs[-1], record)
            self.assertIs(saved_jobs[0], self.old_jobs[0]); self.assertIs(saved_jobs[1], self.old_jobs[1])
            self.assertEqual(len(self.old_jobs), 2); self.assertEqual(len(saved_jobs), 3)
            self.assertIsNot(saved_jobs, self.old_jobs)
            self.assertIs(self.payload["dead"], self.dead); self.assertIs(self.payload["opaque_extra"], self.extra)
            self.assertEqual(self.load_calls, 1)
            self.assertEqual(self.lock.enters, 1); self.assertEqual(self.lock.exits, [(None, None)])
        def test_existing_defaults_and_timestamp_override_are_unchanged(self):
            for supplied in (False, True):
                with self.subTest(supplied=supplied):
                    job = {"job_id": "ordinary-id"}
                    if supplied:
                        job["created_at"] = "synthetic supplied time"
                    before = dict(job); before_clock = self.clock_calls; before_save = len(self.save_calls)
                    record = self.enqueue(job)
                    self.assertEqual(job, before)
                    self.assertEqual(record["created_at"], "synthetic supplied time" if supplied else "synthetic default time")
                    self.assertEqual(record["status"], "queued")
                    self.assertEqual(record["attempts"], 0)
                    self.assertEqual(record["last_error"], "")
                    self.assertEqual(self.clock_calls, before_clock + 1)
                    self.assertEqual(len(self.save_calls), before_save + 1)
        def test_empty_episode_skips_dedup_and_preserves_caller_mapping(self):
            job = {"job_id": "ordinary-id", "episode_id": "", "opaque_metadata": self.meta}
            before = dict(job)
            record = self.enqueue(job)
            self.assert_id(record, "ordinary-id")
            self.assertEqual(record["episode_id"], "")
            self.assertEqual(job, before)
            self.assertIs(record["opaque_metadata"], self.meta)
            # Existing entries are opaque objects without get(); dedup execution
            # would fail rather than accidentally running a real policy.
            self.assertEqual(len(self.payload["jobs"]), 3)
        def test_fake_save_failure_preserves_error_and_exits_context(self):
            failure = OSError("synthetic save failure; no filesystem")
            self.save_failure = failure
            job = {"job_id": "ordinary-id", "opaque_metadata": self.meta}; before = dict(job)
            with self.assertRaises(OSError) as caught:
                self.enqueue(job)
            self.assertIs(caught.exception, failure)
            self.assertEqual(job, before)
            self.assertEqual(len(self.save_calls), 1)
            self.assertEqual(self.lock.enters, 1)
            self.assertEqual(self.lock.exits, [(OSError, failure)])
    return QueueJobIdTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(job_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
