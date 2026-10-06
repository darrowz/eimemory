"""Detached local regression checks; no project import, SQLite, or real traversal."""
import ast
import sys
import types
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "eimemory/adapters/openclaw/qmd_compat.py"
if len(sys.argv) > 1 and sys.argv[1].startswith("--source="):
    SOURCE = Path(sys.argv.pop(1).split("=", 1)[1])


def extracted_methods():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    runtime = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "QmdCompatRuntime")
    nodes = [n for n in runtime.body if isinstance(n, ast.FunctionDef) and n.name in {"_connect", "_list_files"}]
    assert len(nodes) == 2
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), "<detached-qmd-methods>", "exec"), namespace)
    return namespace


class FakeConnection:
    def __init__(self, fail_at=None, error=None, close_error=None):
        self.calls = []
        self.fail_at = fail_at
        self.error = error
        self.close_error = close_error
        self.executions = 0

    def event(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise self.error

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.event("row_factory")
        self._row_factory = value

    def execute(self, sql):
        self.executions += 1
        self.event("execute" + str(self.executions))
        return self

    def commit(self):
        self.event("commit")

    def close(self):
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error


class FakePath:
    def __init__(self, label, *, exists=True, file=True):
        self.label = label
        self.present = exists
        self.file = file
        self.globs = {}
        self.children = {}
        self.calls = []

    def exists(self):
        self.calls.append("exists")
        return self.present

    def is_file(self):
        self.calls.append("is_file")
        return self.file

    def glob(self, pattern):
        self.calls.append(("glob", pattern))
        return self.globs.get(pattern, [])

    def __truediv__(self, pattern):
        self.calls.append(("literal", pattern))
        return self.children.get(pattern, FakePath(pattern, exists=False))

    def __lt__(self, other):
        return self.label < other.label


class LocalRegression(unittest.TestCase):
    def setUp(self):
        self.ns = extracted_methods()
        self.self_stub = types.SimpleNamespace(index_path=object())

    def connect_with(self, conn=None, connect_error=None):
        calls = []
        def connect(path):
            calls.append(path)
            if connect_error is not None:
                raise connect_error
            return conn
        self.ns["sqlite3"] = types.SimpleNamespace(connect=connect, Row=object())
        return calls

    def test_success_transfers_same_connection_without_close(self):
        conn = FakeConnection()
        calls = self.connect_with(conn)
        self.assertIs(self.ns["_connect"](self.self_stub), conn)
        self.assertEqual(conn.calls, ["row_factory", "execute1", "execute2", "commit"])
        self.assertEqual(calls, [self.self_stub.index_path])

    def test_connect_failure_has_no_handle_cleanup(self):
        error = RuntimeError("acquire")
        calls = self.connect_with(connect_error=error)
        with self.assertRaises(RuntimeError) as caught:
            self.ns["_connect"](self.self_stub)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(calls), 1)

    def test_every_initialization_failure_closes_once_and_preserves_original(self):
        for stage in ("row_factory", "execute1", "execute2", "commit"):
            for error_type in (RuntimeError, KeyboardInterrupt, SystemExit):
                for cleanup_type in (None, RuntimeError, KeyboardInterrupt, SystemExit):
                    with self.subTest(stage=stage, error=error_type, cleanup=cleanup_type):
                        original = error_type("original")
                        cleanup = None if cleanup_type is None else cleanup_type("cleanup")
                        conn = FakeConnection(stage, original, cleanup)
                        self.connect_with(conn)
                        with self.assertRaises(BaseException) as caught:
                            self.ns["_connect"](self.self_stub)
                        self.assertIs(caught.exception, original)
                        self.assertEqual(conn.calls.count("close"), 1)
                        self.assertEqual(conn.calls[-2:], [stage, "close"])

    def listing(self, base, pattern):
        self.ns["Path"] = lambda supplied: base
        record = types.SimpleNamespace(path="inert", pattern=pattern)
        return self.ns["_list_files"](self.self_stub, record)

    def test_character_class_patterns_route_to_glob(self):
        for pattern in ("[ab].md", "[a-z].md", "[!a].md"):
            with self.subTest(pattern=pattern):
                base = FakePath("base")
                a, b, directory = FakePath("a.md"), FakePath("b.md"), FakePath("dir", file=False)
                base.globs[pattern] = [b, directory, a]
                self.assertEqual(self.listing(base, pattern), [a, b])
                self.assertIn(("glob", pattern), base.calls)
                self.assertNotIn(("literal", pattern), base.calls)

    def test_star_question_and_recursive_fallback_preserved(self):
        for pattern in ("*.md", "?.md", "**/*.md", "**/[ab].md"):
            with self.subTest(pattern=pattern):
                base, item = FakePath("base"), FakePath("a.md")
                target = pattern.removeprefix("**/")
                base.globs[target] = [item]
                self.assertEqual(self.listing(base, pattern), [item])
                expected = [("glob", pattern)]
                if target != pattern:
                    expected.append(("glob", target))
                self.assertEqual([x for x in base.calls if isinstance(x, tuple)], expected)

    def test_literal_path_and_missing_base_preserved(self):
        base, item = FakePath("base"), FakePath("plain.md")
        base.children["plain.md"] = item
        self.assertEqual(self.listing(base, "plain.md"), [item])
        self.assertIn(("literal", "plain.md"), base.calls)
        self.assertEqual(self.listing(base, "missing.md"), [])
        absent = FakePath("absent", exists=False)
        self.assertEqual(self.listing(absent, "[ab].md"), [])
        self.assertEqual(absent.calls, ["exists"])

    def test_empty_pattern_defaults_and_nonempty_recursive_does_not_retry(self):
        base, item = FakePath("base"), FakePath("a.md")
        base.globs["**/*.md"] = [item]
        self.assertEqual(self.listing(base, ""), [item])
        self.assertEqual(base.calls, ["exists", ("glob", "**/*.md")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
