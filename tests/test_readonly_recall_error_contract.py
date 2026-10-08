"""Isolated local contract tests: extracted function, stdlib, and fake dependencies.

Run from the repository root with python -B -m unittest discover -s tests
-p test_readonly_recall_error_contract.py. For audit fixtures, execute this file
with --source FILE; all expectations remain identical. No target module import,
real pathlib operation, SQLite access, or feature environment read is performed.
These tests establish conditional control flow only, not SQLite integration.
"""
import __future__
import ast
import os.path
import sys
import types
import unittest


SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "eimemory", "storage", "readonly_recall.py")


class FakeSQLiteError(Exception):
    pass


class Interrupt(BaseException):
    pass


class World:
    """All effects made by the extracted function are recorded or injected."""

    def __init__(self, enabled=True, is_file=True, failures=None):
        self.enabled = enabled
        self.is_file_result = is_file
        self.failures = failures or {}
        self.events = []
        self.input = object()
        self.row = object()
        world = self

        class FakePath:
            def resolve(self):
                world.touch("resolve")
                return self

            def is_file(self):
                world.touch("is_file")
                return world.is_file_result

            def as_uri(self):
                world.touch("as_uri")
                return "file:///fake%20database.sqlite"

        class FakeConnection:
            def __setattr__(self, name, value):
                if name != "row_factory":
                    raise AssertionError("Unexpected connection attribute")
                world.touch("row_factory", value)
                object.__setattr__(self, name, value)

            def execute(self, sql):
                world.touch("execute", sql)
                return object()

            def close(self):
                world.touch("close")

        self.path = FakePath()
        self.conn = FakeConnection()
        self.sqlite = types.SimpleNamespace(Error=FakeSQLiteError, Row=self.row,
                                            connect=self.connect)

    def touch(self, stage, *args):
        self.events.append((stage,) + args)
        if stage in self.failures:
            raise self.failures[stage]

    def flag(self):
        self.touch("flag")
        return self.enabled

    def path_factory(self, argument):
        self.touch("Path", argument)
        return self.path

    def connect(self, database, **kwargs):
        self.touch("connect", database, kwargs)
        return self.conn

    def invoke(self, code):
        namespace = {"Path": self.path_factory, "sqlite3": self.sqlite,
                     "readonly_recall_enabled": self.flag,
                     "__builtins__": {"OSError": OSError}}
        exec(code, namespace)
        return namespace["open_readonly_connection"](self.input)

    def expected(self, through="execute"):
        events = [("flag",), ("Path", self.input), ("resolve",), ("is_file",),
                  ("as_uri",),
                  ("connect", "file:///fake%20database.sqlite?mode=ro",
                   {"uri": True, "timeout": 30, "check_same_thread": False}),
                  ("row_factory", self.row), ("execute", "PRAGMA query_only=ON"),
                  ("close",)]
        return events[:[e[0] for e in events].index(through) + 1]


class ReadonlyRecallErrorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reading source bytes is a harness input, never a target Path operation.
        with open(SOURCE, "rb") as source_file:
            source = source_file.read()
        tree = ast.parse(source, filename=SOURCE)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "open_readonly_connection"]
        if len(functions) != 1:
            raise AssertionError("Expected exactly one target function")
        function = functions[0]
        if function.decorator_list:
            raise AssertionError("Unexpected decorators")
        module = ast.Module(body=[function], type_ignores=[])
        ast.fix_missing_locations(module)
        cls.code = compile(module, SOURCE, "exec",
                           flags=__future__.annotations.compiler_flag, dont_inherit=True)

    def assert_returns_none(self, world, through):
        self.assertIsNone(world.invoke(self.code))
        self.assertEqual(world.events, world.expected(through))

    def assert_propagates(self, world, error, through):
        with self.assertRaises(type(error)) as caught:
            world.invoke(self.code)
        self.assertIs(caught.exception, error)
        self.assertEqual(world.events, world.expected(through))

    def test_off_touches_only_flag(self):
        poison = {stage: AssertionError(stage) for stage in
                  ("Path", "resolve", "is_file", "as_uri", "connect",
                   "row_factory", "execute", "close")}
        self.assert_returns_none(World(enabled=False, failures=poison), "flag")

    def test_missing_file_stops_before_uri(self):
        self.assert_returns_none(World(is_file=False), "is_file")

    def test_success_exact_order_uri_kwargs_row_and_connection_identity(self):
        world = World()
        self.assertIs(world.invoke(self.code), world.conn)
        self.assertIs(world.conn.row_factory, world.row)
        self.assertEqual(world.events, world.expected())

    def test_path_oserror_returns_none(self):
        for stage in ("Path", "resolve", "is_file", "as_uri"):
            with self.subTest(stage=stage):
                self.assert_returns_none(World(failures={stage: OSError(stage)}), stage)

    def test_path_oserror_subclass_returns_none(self):
        for stage in ("Path", "resolve", "is_file", "as_uri"):
            with self.subTest(stage=stage):
                self.assert_returns_none(World(failures={stage: PermissionError(stage)}), stage)

    def test_path_non_oserrors_preserve_identity_and_stop(self):
        for stage in ("Path", "resolve", "is_file", "as_uri"):
            for error_type in (ValueError, FakeSQLiteError, Interrupt):
                with self.subTest(stage=stage, error_type=error_type.__name__):
                    error = error_type(stage)
                    self.assert_propagates(World(failures={stage: error}), error, stage)

    def test_flag_errors_are_outside_new_catches(self):
        for error_type in (OSError, FakeSQLiteError, ValueError, Interrupt):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("flag")
                self.assert_propagates(World(failures={"flag": error}), error, "flag")

    def test_connect_sqlite_error_returns_none_without_cleanup(self):
        self.assert_returns_none(World(failures={"connect": FakeSQLiteError()}), "connect")

    def test_connect_other_errors_propagate_without_cleanup(self):
        for error_type in (OSError, ValueError, Interrupt):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("connect")
                self.assert_propagates(World(failures={"connect": error}), error, "connect")

    def test_row_factory_errors_still_propagate_without_cleanup(self):
        for error_type in (FakeSQLiteError, OSError, ValueError, Interrupt):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("row_factory")
                self.assert_propagates(World(failures={"row_factory": error}), error, "row_factory")

    def test_execute_sqlite_error_closes_once_and_returns_none(self):
        self.assert_returns_none(World(failures={"execute": FakeSQLiteError()}), "close")

    def test_execute_sqlite_error_secondary_sqlite_error_returns_none(self):
        self.assert_returns_none(World(failures={"execute": FakeSQLiteError("primary"),
                                                "close": FakeSQLiteError("secondary")}), "close")

    def test_secondary_sqlite_error_subclass_returns_none(self):
        class DerivedSQLiteError(FakeSQLiteError):
            pass
        self.assert_returns_none(World(failures={"execute": DerivedSQLiteError("primary"),
                                                "close": DerivedSQLiteError("secondary")}), "close")

    def test_execute_other_errors_propagate_without_cleanup(self):
        for error_type in (OSError, ValueError, Interrupt):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("execute")
                self.assert_propagates(World(failures={"execute": error}), error, "execute")

    def test_secondary_non_sqlite_errors_still_propagate(self):
        for error_type in (OSError, ValueError, Interrupt):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("close")
                world = World(failures={"execute": FakeSQLiteError("primary"), "close": error})
                self.assert_propagates(world, error, "close")


if __name__ == "__main__":
    if "--source" in sys.argv:
        index = sys.argv.index("--source")
        SOURCE = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    unittest.main()
