"""Stdlib-only AST-isolated CLI regression: never import project/runtime code."""
import argparse
import ast
import contextlib
import io
import json
from pathlib import Path
import sys
import types


def load_main(source):
    tree = ast.parse(source.read_text(encoding='utf-8'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    allowed_calls = {'argparse.ArgumentParser', 'parser.add_argument', 'parser.parse_args',
                     'sys.stdin.read', 'len', 'raw.encode', 'OpenClawRuntimeError', 'json.loads',
                     'verify_openclaw_plugin_runtime', 'parser.exit', 'print'}
    calls = {ast.unparse(node.func) for node in ast.walk(main) if isinstance(node, ast.Call)}
    assert calls <= allowed_calls, calls - allowed_calls
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(main))
    print('staticpreflight: isolated main only; stdlib globals; verifier inert; no project imports')
    class Error(RuntimeError):
        pass
    calls_seen = []
    def inert_verifier(payload, **kwargs):
        calls_seen.append((payload, kwargs))
        return {'hook_count': 3, 'tool_count': 1}
    fake_sys = types.SimpleNamespace(stdin=None)
    globals_ = {'argparse': argparse, 'json': json, 'Path': Path, 'sys': fake_sys,
                'MAX_INSPECT_BYTES': 32, 'OpenClawRuntimeError': Error,
                'verify_openclaw_plugin_runtime': inert_verifier}
    isolated = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), main], type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, '<isolated-cli>', 'exec'), globals_)
    return globals_['main'], fake_sys, calls_seen


import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'deploy' / 'verify_openclaw_plugin_runtime.py'

class OpenClawRuntimeCLILocalTests(unittest.TestCase):
    def setUp(self):
        self.main, self.fake_sys, self.calls = load_main(SOURCE)

    def invoke(self, stream):
        self.fake_sys.stdin = stream
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = self.main(['--expected-root', '/inert/never-resolved'])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def assert_rejected(self, stream):
        status, out, err = self.invoke(stream)
        self.assertEqual(status, 2)
        self.assertEqual(out, '')
        self.assertIn('OpenClaw runtime verification failed:', err)
        self.assertEqual(self.calls, [])

    def test_stdin_decode_error_is_reported(self):
        class DecodeFailure:
            def read(self, size):
                raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')
        self.assert_rejected(DecodeFailure())

    def test_bad_json_is_reported(self):
        self.assert_rejected(io.StringIO('{'))

    def test_oversize_is_reported(self):
        self.assert_rejected(io.StringIO('x' * 33))

    def test_valid_input_calls_inert_verifier(self):
        status, out, err = self.invoke(io.StringIO('{}'))
        self.assertEqual(status, 0)
        self.assertEqual(out, 'openclaw_plugin_runtime=ok hooks=3 tools=1\n')
        self.assertEqual(err, '')
        self.assertEqual(self.calls, [({}, {'expected_root': Path('/inert/never-resolved'), 'allow_legacy_runtime': False})])

if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--source':
        SOURCE = Path(sys.argv[2])
        del sys.argv[1:3]
    unittest.main()
