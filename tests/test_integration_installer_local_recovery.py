"""Stdlib-only AST extraction. All target I/O, imports, locks, and paths are inert."""
import ast
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

class ProbeError(Exception):
    pass

class FakePath:
    name = 'config'
    def __init__(self, value='config'):
        self.value = value
        self.unlinked = False
    @property
    def parent(self):
        return self
    def exists(self):
        return True
    def stat(self):
        return SimpleNamespace(st_mode=0o600)
    def mkdir(self, **kwargs):
        pass
    def unlink(self, **kwargs):
        self.unlinked = True

class FakeHandle:
    def __init__(self, env, closefd):
        self.env, self.closefd = env, closefd
    def __enter__(self):
        return self
    def __exit__(self, *args):
        if self.closefd:
            self.env.close(41)
    def write(self, value):
        pass
    def flush(self):
        self.env.fail('flush')
    def fileno(self):
        return 41

class Env:
    def __init__(self, fault='', platform='posix'):
        self.fault, self.name = fault, platform
        self.closed, self.replaced = [], 0
        self.lock_calls = 0
        self.path = FakePath('temporary')
        for key in ('O_CREAT', 'O_RDWR', 'O_NOFOLLOW', 'SEEK_SET'):
            setattr(self, key, 0)
    def fail(self, stage):
        if self.fault == stage:
            raise ProbeError(stage)
    def close(self, descriptor):
        self.closed.append(descriptor)
    def open(self, *args):
        return 41
    def fdopen(self, *args, closefd=True, **kwargs):
        self.fail('fdopen')
        return FakeHandle(self, closefd)
    def fchmod(self, *args):
        self.fail('fchmod')
    def fsync(self, *args):
        self.fail('fsync')
    def chmod(self, *args):
        pass
    def chown(self, *args):
        pass
    def replace(self, *args):
        self.replaced += 1
    def fstat(self, *args):
        return SimpleNamespace(st_size=1)
    def lseek(self, *args):
        pass
    def write(self, *args):
        pass
    def locking(self, *args):
        self.lock_calls += 1
        if self.lock_calls == 2:
            self.fail('unlock')
    flock = locking

class RemoveImports(ast.NodeTransformer):
    def visit_Import(self, node):
        return ast.copy_location(ast.Pass(), node)
    def visit_ImportFrom(self, node):
        return ast.copy_location(ast.Pass(), node)

def extract(path, function, env):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    node = RemoveImports().visit(node)
    # No project module is imported; only this definition is compiled. Embedded imports
    # are replaced by pass and all effectful referenced globals are inert doubles.
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    ns = {'os': env, 'Path': lambda value: env.path,
          'tempfile': SimpleNamespace(mkstemp=lambda **kw: (41, 'temporary')),
          'stat': SimpleNamespace(S_IMODE=lambda value: value),
          'yaml': SimpleNamespace(safe_dump=lambda *a, **kw: env.fail('serialize')),
          'json': SimpleNamespace(dump=lambda *a, **kw: env.fail('serialize')),
          '_fsync_directory': lambda *a: None,
          '_read_config': lambda *a: ({}, SimpleNamespace(st_dev=1, st_ino=2), b'before'),
          'OpenClawBundledBridgeError': RuntimeError,
          '_LOCAL_CONFIG_LOCK': contextlib.nullcontext(),
          'contextmanager': contextlib.contextmanager,
          'fcntl': SimpleNamespace(flock=env.flock, LOCK_EX=1, LOCK_UN=2),
          'msvcrt': SimpleNamespace(locking=env.locking, LK_LOCK=1, LK_UNLCK=2)}
    exec(compile(ast.fix_missing_locations(module), '<isolated-resource-probe>', 'exec'), ns)
    return ns[function]

def run(root, baseline=False):
    suffix = '.baseline' if baseline else ''
    results = []
    for filename in ('ensure_openclaw_bundled_bridge.py', 'install_hermes_integration.py'):
        path = root / (filename + suffix)
        for fault in ('', 'fdopen', 'serialize', 'flush', 'fsync') + (('fchmod',) if filename.startswith('install') else ()):
            env = Env(fault)
            fn = extract(path, '_write_config', env)
            try:
                if filename.startswith('ensure'):
                    fn(FakePath(), {}, SimpleNamespace(st_mode=0o600, st_uid=1, st_gid=1, st_dev=1, st_ino=2), b'before')
                else:
                    fn(FakePath(), {})
                raised = None
            except ProbeError as exc:
                raised = str(exc)
            ok = env.closed == [41] and raised == (fault or None) and env.replaced == (0 if fault else 1) and env.path.unlinked
            results.append({'case': filename + ':' + (fault or 'success'), 'ok': ok, 'closed': env.closed, 'exception': raised})
    for platform in ('posix', 'nt'):
        for fault in ('', 'unlock', 'body'):
            env = Env(fault, platform)
            fn = extract(root / ('ensure_openclaw_bundled_bridge.py' + suffix), '_config_lock', env)
            try:
                with fn(FakePath()):
                    env.fail('body')
                raised = None
            except ProbeError as exc:
                raised = str(exc)
            results.append({'case': 'lock:' + platform + ':' + (fault or 'success'), 'ok': env.closed == [41] and raised == (fault or None), 'closed': env.closed, 'exception': raised})
    return results

class IntegrationInstallerLocalRecoveryTests(unittest.TestCase):
    def test_inert_resource_lifetimes(self):
        root = Path(__file__).resolve().parents[1] / "deploy"
        results = run(root)
        self.assertEqual(len(results), 17)
        for result in results:
            with self.subTest(case=result["case"]):
                self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
