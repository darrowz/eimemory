"""Whole-source inert tests: every project import, OS call and path probe is fake."""
import argparse
import builtins
import contextlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).parents[1]


def load_inert(relative, *, environment=None, existing=(), script=False):
    state = types.SimpleNamespace(probes=[], existing=set(existing), expanded=[])

    class InertPath(PurePosixPath):
        def resolve(self):
            return self

        def expanduser(self):
            state.expanded.append(str(self))
            return InertPath(str(self).replace('~/', '/fake-home/', 1))

        def exists(self):
            state.probes.append(str(self))
            return str(self) in state.existing

    fake_sys = types.SimpleNamespace(path=['original'], dont_write_bytecode=False)
    handle = Mock()
    handle.__enter__ = Mock(return_value=handle)
    handle.__exit__ = Mock(return_value=False)
    handle.fileno.return_value = 7
    handle.read.return_value = b'{}'
    fake_os = types.SimpleNamespace(environ=dict(environment or {}), O_RDONLY=0,
                                   O_CLOEXEC=1, O_NOFOLLOW=2, O_NONBLOCK=4,
                                   open=Mock(return_value=7), fdopen=Mock(return_value=handle),
                                   fstat=Mock(return_value=types.SimpleNamespace(st_mode=stat.S_IFREG, st_size=2)))
    strict = Mock(return_value={'parsed': True})
    summary = Mock(return_value={'exit_code': 0, 'label': '中文'})
    loop_main = Mock(return_value=17)
    contracts = types.SimpleNamespace(**{name: Mock() for name in (
        'acceptance_failure_details', 'channel_wait_report_ok', 'live_acceptance_report_ok',
        'legacy_release_replay_ok')})
    verdict = types.SimpleNamespace(summarize_release_closure=summary, **{name: Mock() for name in (
        '_reported_release_summary', '_release_closure_summary_contract_ok',
        '_deployment_identity_matches', '_release_authority_matches', '_exact_int')})
    loop = types.SimpleNamespace(__all__=['main', 'public_token'], main=loop_main, public_token=42)
    modules = {'argparse': argparse, 'json': json, 'os': fake_os, 'pathlib': types.SimpleNamespace(Path=InertPath),
               're': types.SimpleNamespace(), 'stat': stat, 'sys': fake_sys,
               'typing': types.SimpleNamespace(Any=object),
               'eimemory.governance.release.closure_contracts': contracts,
               'eimemory.governance.release.closure_verdict': verdict,
               'eimemory.core.strict_json': types.SimpleNamespace(loads=strict),
               'eimemory.ops.openclaw_loop': loop,
               '__future__': __import__('__future__')}

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in modules:
            raise AssertionError('unapproved import: ' + name)
        return modules[name]

    safe_builtins = dict(vars(builtins), __import__=safe_import)
    for name in ('open', 'eval', 'exec', 'input'):
        safe_builtins[name] = Mock(side_effect=AssertionError('unapproved builtin: ' + name))
    namespace = {'__builtins__': safe_builtins, '__name__': '__main__' if script else 'inert_module',
                 '__file__': '/inert/repo/' + relative}
    source = (ROOT / relative).read_text(encoding='utf-8')
    state.os, state.sys, state.handle = fake_os, fake_sys, handle
    state.strict, state.summary, state.loop_main = strict, summary, loop_main
    state.Path = InertPath
    try:
        exec(compile(source, relative, 'exec'), namespace)
    except SystemExit as exc:
        state.exit_code = exc.code
    return namespace, state


class ReleaseLoopHelpersTests(unittest.TestCase):
    def release(self):
        return load_inert('deploy/summarize_release_closure.py')

    def test_reader_success_and_exact_boundary(self):
        ns, s = self.release()
        self.assertTrue(s.sys.dont_write_bytecode)
        self.assertEqual(s.sys.path[0], '/inert/repo')
        s.os.fstat.return_value.st_size = ns['MAX_REPORT_BYTES']
        result = ns['_read_report'](s.Path('/synthetic/report'))
        self.assertEqual(result, {'parsed': True})
        s.os.open.assert_called_once_with(s.Path('/synthetic/report'), 7)
        s.os.fdopen.assert_called_once_with(7, 'rb', closefd=True)
        s.handle.read.assert_called_once_with(ns['MAX_REPORT_BYTES'] + 1)
        s.strict.assert_called_once_with(b'{}', max_bytes=ns['MAX_REPORT_BYTES'], max_depth=64)
        s.handle.__exit__.assert_called_once()

    def test_reader_metadata_rejections(self):
        for mode, size in ((stat.S_IFDIR, 0), (stat.S_IFREG, 16 * 1024 * 1024 + 1)):
            with self.subTest(mode=mode, size=size):
                ns, s = self.release()
                s.os.fstat.return_value = types.SimpleNamespace(st_mode=mode, st_size=size)
                with self.assertRaises(ValueError):
                    ns['_read_report'](s.Path('/synthetic'))
                s.handle.read.assert_not_called()
                s.handle.__exit__.assert_called_once()
                s.strict.assert_not_called()

    def test_reader_growth_rejected_after_close(self):
        ns, s = self.release()
        ns['MAX_REPORT_BYTES'] = 3
        s.handle.read.return_value = b'1234'
        with self.assertRaises(ValueError):
            ns['_read_report'](s.Path('/synthetic'))
        s.handle.__exit__.assert_called_once()
        s.strict.assert_not_called()

    def test_reader_error_propagation(self):
        for point in ('open', 'fdopen', 'fstat', 'read', 'strict'):
            with self.subTest(point=point):
                ns, s = self.release()
                mock = s.strict if point == 'strict' else (s.handle.read if point == 'read' else getattr(s.os, point))
                mock.side_effect = OSError('synthetic')
                with self.assertRaises(OSError):
                    ns['_read_report'](s.Path('/synthetic'))
                if point in ('fstat', 'read', 'strict'):
                    s.handle.__exit__.assert_called_once()

    def test_cli_success(self):
        ns, s = self.release()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ns['main'](['--path', '/synthetic']), 0)
        self.assertEqual(json.loads(out.getvalue()), {'exit_code': 0, 'label': '中文'})
        self.assertIn('中文', out.getvalue())
        s.summary.assert_called_once_with({'parsed': True})

    def test_cli_expected_errors(self):
        for stage in ('_read_report', 'summarize_release_closure'):
            for error in (OSError('x'), UnicodeError('x'), ValueError('x'), json.JSONDecodeError('x', '', 0)):
                with self.subTest(stage=stage, error=type(error).__name__):
                    ns, _ = self.release()
                    ns[stage] = Mock(side_effect=error)
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                        ns['main'](['--path', '/synthetic'])
                    self.assertEqual(caught.exception.code, 2)
                    self.assertEqual(out.getvalue(), '')
                    self.assertIn('release closure summary failed:', err.getvalue())

    def test_cli_missing_argument(self):
        ns, s = self.release()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            ns['main']([])
        self.assertEqual(caught.exception.code, 2)
        s.os.open.assert_not_called()

    def test_cli_output_contract_failures_propagate(self):
        for summary, exception in (({'exit_code': 0, 'bad': float('nan')}, ValueError), ({}, KeyError)):
            ns, s = self.release()
            s.summary.return_value = summary
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(exception):
                ns['main'](['--path', '/synthetic'])

    def test_loop_order_expansion_deduplication(self):
        ns, s = load_inert('scripts/openclaw_loop.py', environment={
            'OPENCLAW_LOOP_REPO': '~/chosen', 'EIMEMORY_REPO': '/fake-home/chosen', 'EIMEMORY_HOME': ''})
        self.assertEqual([str(x) for x in ns['_candidate_roots']()],
                         ['/fake-home/chosen', '/inert/repo', '/inert/repo/eimemory', '/dev-project/eimemory', '/opt/eimemory/current'])
        self.assertEqual(s.sys.path, ['original'])
        self.assertEqual(ns['public_token'], 42)
        s.loop_main.assert_not_called()

    def test_loop_first_match_and_main_guard(self):
        ns, s = load_inert('scripts/openclaw_loop.py', existing={
            '/inert/repo/eimemory/ops/openclaw_loop.py', '/dev-project/eimemory/eimemory/ops/openclaw_loop.py'}, script=True)
        self.assertEqual(s.probes, ['/inert/repo/eimemory/ops/openclaw_loop.py'])
        self.assertEqual(s.sys.path, ['/inert/repo', 'original'])
        self.assertEqual(s.exit_code, 17)
        s.loop_main.assert_called_once_with()
        self.assertIn('/home/darrow/.openclaw/workspace/eimemory', [str(x) for x in ns['_candidate_roots']()])


if __name__ == '__main__':
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument('--source-root', type=Path)
    args, remaining = options.parse_known_args()
    if args.source_root is not None:
        ROOT = args.source_root
    unittest.main(argv=[sys.argv[0], *remaining])
