"""Isolated control-flow regression; no real target filesystem or descriptors."""
import ast
from pathlib import Path as SourcePath
from types import SimpleNamespace
import unittest


def load_write():
    source = SourcePath(__file__).resolve().parents[1] / "deploy" / "ensure_attestation_profile.py"
    tree = ast.parse(source.read_bytes(), filename=str(source))
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "_atomic_private_write"]
    if len(matches) != 1 or matches[0].decorator_list:
        raise AssertionError("expected one undecorated target function")
    # Execute only this function definition. No project imports or module body.
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), matches[0]], type_ignores=[])
    return compile(ast.fix_missing_locations(module), str(source), "exec")


class Fault(Exception):
    pass


class Stop(BaseException):
    pass


class FakePath:
    def __init__(self, world, label):
        self.world = world
        self.label = label
        self.name = "profile.json"

    @property
    def parent(self):
        return self.world.parent

    def mkdir(self, **kwargs):
        self.world.event("mkdir", self.label, kwargs)

    def is_symlink(self):
        self.world.event("is_symlink", self.label)
        return self.world.symlink

    def exists(self):
        self.world.event("exists", self.label)
        return self.world.present

    def unlink(self):
        self.world.event("unlink", self.label)
        self.world.present = False

    def __str__(self):
        return self.label


class FakeHandle:
    def __init__(self, world):
        self.world = world

    def __enter__(self):
        self.world.event("enter")
        return self

    def __exit__(self, kind, value, traceback):
        self.world.event("exit", None if kind is None else kind.__name__)
        return False

    def write(self, content):
        self.world.event("write", content)

    def flush(self):
        self.world.event("flush")

    def fileno(self):
        self.world.event("fileno")
        return self.world.raw


class World:
    def __init__(self, name="posix", failures=None, present=True, symlink=False):
        self.events = []
        self.failures = {} if failures is None else failures
        self.present = present
        self.symlink = symlink
        self.raw = object()
        self.parent_fd = object()
        self.parent = FakePath(self, "parent")
        self.target = FakePath(self, "target")
        self.temporary = FakePath(self, "temporary")
        self.handle = FakeHandle(self)
        self.os = SimpleNamespace(name=name, O_RDONLY=0x10, O_DIRECTORY=0x80,
            fchmod=self.fchmod, fchown=self.fchown, fdopen=self.fdopen,
            fsync=self.fsync, replace=self.replace, open=self.open, close=self.close)

    def event(self, name, *args):
        self.events.append((name,) + args)
        if name in self.failures:
            raise self.failures[name]

    def descriptor_name(self, descriptor):
        if descriptor is self.raw:
            return "raw"
        if descriptor is self.parent_fd:
            return "parent_fd"
        raise AssertionError("unexpected descriptor")

    def path(self, name):
        self.event("Path", name)
        if name != "temporary-name":
            raise AssertionError("unexpected temporary name")
        return self.temporary

    def mkstemp(self, *, prefix, dir):
        self.event("mkstemp", prefix, dir.label)
        return self.raw, "temporary-name"

    def fchmod(self, descriptor, mode):
        self.event("fchmod", self.descriptor_name(descriptor), mode)

    def fchown(self, descriptor, uid, gid):
        self.event("fchown", self.descriptor_name(descriptor), uid, gid)

    def fdopen(self, descriptor, mode, **kwargs):
        self.event("fdopen", self.descriptor_name(descriptor), mode, kwargs)
        return self.handle

    def fsync(self, descriptor):
        self.event(self.descriptor_name(descriptor) + ".fsync")

    def replace(self, source, target):
        self.event("replace", source.label, target.label)
        self.present = False

    def open(self, path, flags):
        self.event("open", path.label, flags)
        return self.parent_fd

    def close(self, descriptor):
        self.event(self.descriptor_name(descriptor) + ".close")

    def run(self, code, uid=11, gid=22):
        namespace = {"Path": self.path, "tempfile": SimpleNamespace(mkstemp=self.mkstemp),
                     "os": self.os}
        exec(code, namespace)
        return namespace["_atomic_private_write"](self.target, "opaque\n", uid=uid, gid=gid)


START = [("mkdir", "parent", {"parents": True, "exist_ok": True}),
         ("is_symlink", "parent"), ("mkstemp", ".profile.json.", "parent"),
         ("Path", "temporary-name")]
MODE = [("fchmod", "raw", 0o600)]
OWNER = [("fchown", "raw", 11, 22)]
IO = [("fdopen", "raw", "w", {"encoding": "utf-8", "newline": "\n"}),
      ("enter",), ("write", "opaque\n"), ("flush",), ("fileno",),
      ("raw.fsync",), ("exit", None), ("replace", "temporary", "target")]
PARENT = [("open", "parent", 0x90), ("parent_fd.fsync",), ("parent_fd.close",)]
CLEAN = [("exists", "temporary"), ("unlink", "temporary")]


class DescriptorCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = load_write()

    def raised(self, world, exception, expected):
        with self.assertRaises(type(exception)) as caught:
            world.run(self.code)
        self.assertIs(caught.exception, exception)
        self.assertEqual(world.events, expected)
        return caught.exception

    def test_success_exact_order_and_owner_conditions(self):
        for name in ("posix", "nt"):
            for uid, gid in ((11, 22), (None, 22), (11, None), (None, None),
                             (0, 0), (0, 22), (11, 0)):
                with self.subTest(name=name, uid=uid, gid=gid):
                    world = World(name=name)
                    owner = [("fchown", "raw", uid, gid)] if (
                        name == "posix" and uid is not None and gid is not None) else []
                    self.assertIsNone(world.run(self.code, uid=uid, gid=gid))
                    self.assertEqual(world.events, START + MODE + owner + IO +
                        (PARENT if name == "posix" else []) + CLEAN[:1])

    def test_pre_handoff_failures_close_raw_and_reraise(self):
        for stage, prefix in (("fchmod", START + MODE),
                              ("fchown", START + MODE + OWNER)):
            for kind in (Fault, Stop, KeyboardInterrupt, SystemExit):
                with self.subTest(stage=stage, kind=kind.__name__):
                    error = kind("pre-handoff")
                    self.raised(World(failures={stage: error}), error,
                                prefix + [("raw.close",)] + CLEAN)

    def test_raw_close_failure_still_runs_outer_cleanup_and_chains(self):
        for stage, prefix in (("fchmod", START + MODE),
                              ("fchown", START + MODE + OWNER)):
            for close_kind in (Fault, Stop):
                with self.subTest(stage=stage, close_kind=close_kind.__name__):
                    first, close = Stop("pre-handoff"), close_kind("close")
                    caught = self.raised(World(failures={stage: first, "raw.close": close}),
                        close, prefix + [("raw.close",)] + CLEAN)
                    self.assertIs(caught.__context__, first)
                    self.assertIsNone(caught.__cause__)

    def test_pre_handoff_absent_temporary_does_not_unlink(self):
        error = Fault("mode")
        self.raised(World(present=False, failures={"fchmod": error}), error,
                    START + MODE + [("raw.close",)] + CLEAN[:1])

    def test_pre_handoff_outer_cleanup_errors_keep_existing_precedence(self):
        for stage, tail in (("exists", CLEAN[:1]), ("unlink", CLEAN)):
            for close_fails in (False, True):
                with self.subTest(stage=stage, close_fails=close_fails):
                    first, close, cleanup = Fault("mode"), Fault("close"), Stop("cleanup")
                    failures = {"fchmod": first, stage: cleanup}
                    if close_fails:
                        failures["raw.close"] = close
                    caught = self.raised(World(failures=failures), cleanup,
                        START + MODE + [("raw.close",)] + tail)
                    self.assertIs(caught.__context__, close if close_fails else first)
                    if close_fails:
                        self.assertIs(close.__context__, first)

    def test_pre_acquisition_and_path_failures_are_unchanged(self):
        for index, stage in enumerate(("mkdir", "is_symlink", "mkstemp", "Path")):
            with self.subTest(stage=stage):
                error = Fault(stage)
                self.raised(World(failures={stage: error}), error, START[:index + 1])
        world = World(symlink=True)
        with self.assertRaisesRegex(RuntimeError, "credential parent must not be a symlink: parent"):
            world.run(self.code)
        self.assertEqual(world.events, START[:2])

    def test_fdopen_enter_and_io_failures_are_not_raw_closed(self):
        for index, stage in enumerate(("fdopen", "enter", "write", "flush", "fileno", "raw.fsync")):
            for kind in (Fault, Stop):
                with self.subTest(stage=stage, kind=kind.__name__):
                    error = kind(stage)
                    suffix = [("exit", kind.__name__)] if index >= 2 else []
                    self.raised(World(failures={stage: error}), error,
                        START + MODE + OWNER + IO[:index + 1] + suffix + CLEAN)

    def test_exit_and_replace_failures_are_unchanged(self):
        for index, stage in ((6, "exit"), (7, "replace")):
            with self.subTest(stage=stage):
                error = Fault(stage)
                self.raised(World(failures={stage: error}), error,
                    START + MODE + OWNER + IO[:index + 1] + CLEAN)

    def test_exit_failure_retains_exception_context(self):
        first, exit_error = Fault("write"), Stop("exit")
        caught = self.raised(World(failures={"write": first, "exit": exit_error}), exit_error,
            START + MODE + OWNER + IO[:3] + [("exit", "Fault")] + CLEAN)
        self.assertIs(caught.__context__, first)

    def test_parent_failures_keep_existing_resource_scope(self):
        for stage, tail in (("open", PARENT[:1]),
                            ("parent_fd.fsync", PARENT), ("parent_fd.close", PARENT)):
            with self.subTest(stage=stage):
                error = Stop(stage)
                self.raised(World(failures={stage: error}), error,
                    START + MODE + OWNER + IO + tail + CLEAN[:1])

    def test_parent_close_error_overrides_fsync_with_context(self):
        first, close = Fault("parent sync"), Stop("parent close")
        caught = self.raised(World(failures={"parent_fd.fsync": first,
            "parent_fd.close": close}), close, START + MODE + OWNER + IO + PARENT + CLEAN[:1])
        self.assertIs(caught.__context__, first)

    def test_outer_exists_error_after_parent_close_keeps_precedence(self):
        first, cleanup = Fault("parent close"), Stop("exists")
        caught = self.raised(World(failures={"parent_fd.close": first, "exists": cleanup}),
            cleanup, START + MODE + OWNER + IO + PARENT + CLEAN[:1])
        self.assertIs(caught.__context__, first)


if __name__ == "__main__":
    unittest.main()
