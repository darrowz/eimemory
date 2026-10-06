"""Bounded AST-only tests; target I/O and platform lock modules are inert doubles."""
import ast
import builtins
from contextlib import contextmanager, nullcontext
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "deploy/ensure_openclaw_bridge_config.py"
if "--source" in sys.argv:
    index = sys.argv.index("--source")
    SOURCE = Path(sys.argv[index + 1])
    del sys.argv[index:index + 2]


class FakePath:
    def __init__(self, value, world=None):
        self.namepath = str(value)
        self.world = world or WORLD

    def __str__(self):
        return self.namepath

    @property
    def parent(self):
        return FakePath(str(PurePosixPath(self.namepath).parent), self.world)

    @property
    def name(self):
        return PurePosixPath(self.namepath).name

    def with_name(self, name):
        return FakePath(str(PurePosixPath(self.namepath).with_name(name)), self.world)

    def mkdir(self, **kwargs):
        self.world.events.append("mkdir")

    def is_symlink(self):
        return False

    def is_file(self):
        return self.namepath in self.world.files

    def stat(self, **kwargs):
        return self.world.metadata(self.namepath)

    def unlink(self, **kwargs):
        self.world.events.append("unlink")
        self.world.files.pop(self.namepath, None)


class Stream(io.StringIO):
    def __init__(self, world, descriptor, mode, closefd):
        self.world, self.descriptor, self.mode, self.closefd = world, descriptor, mode, closefd
        super().__init__(world.files.get(world.fds[descriptor], "") if mode == "r" else "")

    def fileno(self):
        return self.descriptor

    def close(self):
        if not self.closed:
            if self.mode == "w":
                self.world.files[self.world.fds[self.descriptor]] = self.getvalue()
            if self.closefd:
                self.world.close(self.descriptor)
        super().close()


class World:
    O_CREAT, O_RDWR, O_RDONLY, O_DIRECTORY, O_NOFOLLOW, SEEK_SET = 1, 2, 4, 8, 16, 0
    def __init__(self, platform="posix", errors=None):
        self.name = platform
        self.errors = errors or {}
        self.files = {"/config.json": "{}"}
        self.fds = {}
        self.events = []
        self.next_fd = 10
        self.seek_count = 0

    def event(self, name):
        self.events.append(name)
        if name in self.errors:
            raise self.errors[name]

    def open(self, path, *args):
        self.event("open")
        self.next_fd += 1
        self.fds[self.next_fd] = str(path)
        return self.next_fd

    def close(self, descriptor):
        self.event("close")
        del self.fds[descriptor]

    def metadata(self, path):
        return SimpleNamespace(st_size=len(self.files.get(path, "").encode("utf-8")), st_mode=stat.S_IFREG | 0o640, st_uid=17, st_gid=18, st_dev=1, st_ino=2)

    def fstat(self, descriptor):
        return self.metadata(self.fds[descriptor])

    def fdopen(self, descriptor, mode, encoding=None, newline=None, closefd=True):
        self.event("fdopen")
        return Stream(self, descriptor, mode, closefd)

    def fsync(self, descriptor):
        self.event("fsync")

    def chmod(self, path, mode):
        self.event("chmod")
        assert mode == 0o640

    def chown(self, path, uid, gid):
        self.event("chown")
        assert (uid, gid) == (17, 18)

    def replace(self, source, target):
        self.event("replace")
        self.files[str(target)] = self.files.pop(str(source))

    def mkstemp(self, **kwargs):
        self.event("mkstemp")
        name = "/.temporary"
        return self.open(name), name

    def write(self, descriptor, value):
        self.event("write-lock-byte")

    def lseek(self, *args):
        self.seek_count += 1
        self.event("seek-acquire" if self.seek_count == 1 else "seek-release")

    def flock(self, descriptor, operation):
        self.event("acquire" if operation == 1 else "release")

    def locking(self, descriptor, operation, count):
        self.event("acquire" if operation == 1 else "release")


WORLD = None


def load(world):
    global WORLD
    WORLD = world
    source = ast.parse(SOURCE.read_text())
    nodes = [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    modules = {"fcntl": SimpleNamespace(flock=world.flock, LOCK_EX=1, LOCK_UN=2), "msvcrt": SimpleNamespace(locking=world.locking, LK_LOCK=1, LK_UNLCK=2)}
    def inert_import(name, *args, **kwargs):
        if name not in modules:
            raise AssertionError("Unexpected target import: " + name)
        return modules[name]
    safe_builtins = dict(vars(builtins), __import__=inert_import)
    namespace = dict(__builtins__=safe_builtins, os=world, Path=FakePath, stat=stat, json=json, tempfile=world, contextmanager=contextmanager, _LOCAL_CONFIG_LOCK=nullcontext(), MAX_CONFIG_BYTES=4 * 1024 * 1024, PLUGIN_ID="eimemory-bridge")
    # Future annotations avoid evaluating real os types on the inert object.
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes, type_ignores=[])
    # Compile inherits postponed annotations; remove future import before execution.
    import __future__
    unit.body = nodes
    exec(compile(ast.fix_missing_locations(unit), str(SOURCE), "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class LocalRecovery(unittest.TestCase):
    def test_lock_exception_matrix(self):
        for platform in ("posix", "nt"):
            for origin in (None, "body", "acquire"):
                for release in (False, True):
                    for close in (False, True):
                        with self.subTest(platform=platform, origin=origin, release=release, close=close):
                            errors = {}
                            if origin == "acquire": errors["acquire"] = RuntimeError("acquire")
                            if release: errors["release"] = RuntimeError("release")
                            if close: errors["close"] = RuntimeError("close")
                            w = World(platform, errors); ns = load(w)
                            body_error = RuntimeError("body")
                            caught = None
                            try:
                                with ns["_interprocess_lock"](FakePath("/lock")):
                                    if origin == "body": raise body_error
                            except BaseException as exc:
                                caught = exc
                            expected = body_error if origin == "body" else errors.get("acquire") or errors.get("release") or errors.get("close")
                            self.assertIs(caught, expected)
                            self.assertEqual(w.events.count("close"), 1)
                            if not close: self.assertEqual(w.fds, {})

    def test_windows_seek_release_still_closes(self):
        for body_fails in (False, True):
            error, body = RuntimeError("seek"), RuntimeError("body")
            w = World("nt", {"seek-release": error}); ns = load(w)
            with self.assertRaises(RuntimeError) as raised:
                with ns["_interprocess_lock"](FakePath("/lock")):
                    if body_fails: raise body
            self.assertIs(raised.exception, body if body_fails else error)
            self.assertEqual(w.events.count("close"), 1)
            self.assertEqual(w.fds, {})

    def test_acquire_seek_failure_preserves_error(self):
        error = RuntimeError("acquire seek")
        w = World("nt", {"seek-acquire": error, "close": RuntimeError("close")}); ns = load(w)
        with self.assertRaises(RuntimeError) as raised:
            with ns["_interprocess_lock"](FakePath("/lock")): pass
        self.assertIs(raised.exception, error)
        self.assertEqual(w.events.count("close"), 1)

    def test_pretty_format_values_and_metadata(self):
        w = World(); ns = load(w); payload = {"empty": "", "unicode": "中文😀", "values": [None, False, 1, 1.25, {"space": " \n "}]}
        ns["_write_atomic"](FakePath("/config.json"), payload, metadata=w.metadata("/config.json"))
        self.assertEqual(w.files["/config.json"], json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self.assertIn("chmod", w.events); self.assertIn("chown", w.events)
        self.assertEqual(w.fds, {})

    def test_byte_boundaries_with_unicode_and_newline(self):
        for value in ("", "a", "中", "😀", " \t\n "):
            payload = {"v": value}
            compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            pretty = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            for limit in sorted({len(compact.encode()) - 1, len(compact.encode()), len(pretty.encode()) - 1, len(pretty.encode())}):
                with self.subTest(value=value, limit=limit):
                    w = World(); ns = load(w); ns["MAX_CONFIG_BYTES"] = limit
                    if len(compact.encode()) > limit:
                        with self.assertRaises(ns["OpenClawBridgeConfigError"]):
                            ns["_write_atomic"](FakePath("/config.json"), payload, metadata=w.metadata("/config.json"))
                        self.assertEqual(w.files["/config.json"], "{}")
                        self.assertEqual(w.events, [])
                    else:
                        ns["_write_atomic"](FakePath("/config.json"), payload, metadata=w.metadata("/config.json"))
                        actual = w.files["/config.json"]
                        self.assertLessEqual(len(actual.encode()), limit)
                        self.assertEqual(json.loads(actual), payload)
                        self.assertTrue(actual.endswith("\n"))
                        reread, _ = ns["_read_config"](FakePath("/config.json"))
                        self.assertEqual(reread, payload)

    def test_real_cap_pretty_expansion_reentrant(self):
        w = World(); ns = load(w)
        # Input below 4 MiB; 2-space list indentation expands output past 4 MiB.
        payload = {"padding": [0] * 600000, "unicode": "中文😀"}
        w.files["/config.json"] = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self.assertLess(len(w.files["/config.json"].encode()), ns["MAX_CONFIG_BYTES"])
        self.assertGreater(len((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()), ns["MAX_CONFIG_BYTES"])
        first = ns["_ensure_openclaw_bridge_config_locked"](FakePath("/config.json"))
        self.assertTrue(first["changed"])
        self.assertLessEqual(len(w.files["/config.json"].encode()), ns["MAX_CONFIG_BYTES"])
        second = ns["_ensure_openclaw_bridge_config_locked"](FakePath("/config.json"))
        self.assertFalse(second["changed"])

    def test_whitespace_input_and_second_pass(self):
        w = World(); ns = load(w)
        w.files["/config.json"] = ' \t\n {"keep": " \u4e2d ", "channels": null} \r\n'
        ns["MAX_CONFIG_BYTES"] = 300
        first = ns["_ensure_openclaw_bridge_config_locked"](FakePath("/config.json"))
        self.assertTrue(first["changed"])
        self.assertEqual(json.loads(w.files["/config.json"])["keep"], " 中 ")
        self.assertLessEqual(len(w.files["/config.json"].encode()), 300)
        second = ns["_ensure_openclaw_bridge_config_locked"](FakePath("/config.json"))
        self.assertFalse(second["changed"])

    def test_baseexception_body_remains_primary(self):
        for body in (KeyboardInterrupt(), GeneratorExit()):
            w = World(errors={"release": RuntimeError("release"), "close": RuntimeError("close")}); ns = load(w)
            with self.assertRaises(type(body)) as raised:
                with ns["_interprocess_lock"](FakePath("/lock")):
                    raise body
            self.assertIs(raised.exception, body)
            self.assertEqual(w.events.count("close"), 1)

    def test_managed_growth_rejected_without_temporary_write(self):
        w = World(); ns = load(w)
        payload = {"padding": "x" * (ns["MAX_CONFIG_BYTES"] - 20)}
        original = json.dumps(payload, separators=(",", ":"))
        self.assertLessEqual(len(original.encode()), ns["MAX_CONFIG_BYTES"])
        w.files["/config.json"] = original
        with self.assertRaises(ns["OpenClawBridgeConfigError"]):
            ns["_ensure_openclaw_bridge_config_locked"](FakePath("/config.json"))
        self.assertEqual(w.files["/config.json"], original)
        self.assertNotIn("mkstemp", w.events)
        self.assertNotIn("replace", w.events)
        self.assertNotIn("fsync", w.events)


if __name__ == "__main__":
    unittest.main()
