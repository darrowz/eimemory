"""Offline ordinary-flow regressions; never import application modules or use I/O helpers.

Run: python -B tests/snapshot_helpers_inert_shell.py
An optional source root argument permits independent baseline/final comparisons.
"""
import argparse
import ast
import io
import json
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

ROOT = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]


def functions(filename, names, extra):
    tree = ast.parse((ROOT / 'deploy' / filename).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    env = dict(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), env)
    return env


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.read.return_value = b'{"healthy":true}'
        self.opener = Mock()
        self.opener.open.return_value = self.response
        self.env = functions('capture_prior_health_snapshot.py', {'_NoRedirect', '_fetch_health', 'main'}, {
            'HTTPRedirectHandler': object, 'Any': object, 'urlsplit': urlsplit,
            'Request': Mock(), 'rpc_probe_headers': Mock(return_value={}),
            'build_opener': Mock(return_value=self.opener), 'ProxyHandler': Mock(),
            'HTTPError': HTTPError, 'URLError': URLError, 'json': json, 'argparse': argparse,
            'sys': types.SimpleNamespace(stdout=types.SimpleNamespace(buffer=io.BytesIO()), stderr=io.StringIO()),
            'PRIOR_HEALTH_SNAPSHOT_SCHEMA': 'prior_health_snapshot.v1',
            'MAX_PRIOR_HEALTH_SNAPSHOT_BYTES': 65536,
        })

    def test_malformed_url_is_error_value(self):
        for url in ['http://[::1', 'http://[bad]/health']:
            with self.subTest(url=url):
                self.assertEqual(self.env['_fetch_health'](url), {'_fetch_error': 'health_fetch_failed'})
        self.opener.open.assert_not_called()

    def test_malformed_url_cli(self):
        self.assertEqual(self.env['main'](['--health-url', 'http://[::1']), 2)
        self.assertEqual(self.env['sys'].stderr.getvalue(), 'prior_health_capture_failed\n')
        self.assertEqual(self.env['sys'].stdout.buffer.getvalue(), b'')

    def test_nonmatching_url_literal(self):
        for url in ['', None, 'https://127.0.0.1', 'http://example.invalid']:
            self.assertEqual(self.env['_fetch_health'](url), {'_fetch_error': 'health_url_not_loopback_http'})
        self.opener.open.assert_not_called()

    def test_success_and_request_arguments(self):
        self.assertEqual(self.env['_fetch_health']('http://127.0.0.1/health'), {'healthy': True})
        self.response.read.assert_called_once_with(65537)
        self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 5.0})
        self.assertIsNone(self.env['_NoRedirect']().redirect_request(None, None, 302, '', {}, 'x'))

    def test_response_types_and_errors(self):
        for raw, code in [(b'[]', 'health_payload_not_object'), (b'null', 'health_payload_not_object'),
                          (b'\xff', 'health_fetch_failed'), (b'{', 'health_fetch_failed'),
                          (b'x' * 65537, 'health_response_too_large')]:
            self.response.read.return_value = raw
            self.assertEqual(self.env['_fetch_health']('http://[::1]/health'), {'_fetch_error': code})
        self.opener.open.side_effect = URLError('offline stub')
        self.assertEqual(self.env['_fetch_health']('http://127.0.0.1'), {'_fetch_error': 'health_fetch_failed'})

    def test_main_success(self):
        self.assertEqual(self.env['main'](['--health-url', 'http://127.0.0.1/health']), 0)
        raw = self.env['sys'].stdout.buffer.getvalue()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertEqual(json.loads(raw)['health'], {'healthy': True})

    def test_main_serialization_failures(self):
        for payload in [{'x': object()}, {'x': '\ud800'}, {'x': 'x' * 65536}]:
            self.env['_fetch_health'] = Mock(return_value=payload)
            self.assertEqual(self.env['main'](['--health-url', 'http://127.0.0.1']), 2)
        self.assertEqual(self.env['sys'].stdout.buffer.getvalue(), b'')


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.env = functions('rebuild_projection_snapshot.py', {'rebuild', '_build_into', 'main'}, {
            'Path': Path, 'os': Mock(), 'tempfile': Mock(), 'open_snapshot': Mock(),
            'require_real_directory_chain': Mock(), 'atomic_write_json': Mock(),
            'loads': Mock(side_effect=lambda text, **kw: json.loads(text)),
            'argparse': argparse, 'json': json, 'sqlite3': sqlite3,
            'sys': types.SimpleNamespace(stderr=io.StringIO()),
        })
        self.required = ['record_id', 'kind', 'status', 'tenant_id', 'agent_id', 'workspace_id', 'user_id',
                         'source_id', 'payload_json', 'payload_pointer_json', 'storage_key']
        self.record = types.SimpleNamespace(record_id='r', kind='memory', status='active', source_id='s',
            scope=types.SimpleNamespace(tenant_id='t', agent_id='a', workspace_id='w', user_id='u'))
        self.row = dict(record_id='r', kind='memory', status='active', source_id='s', tenant_id='t',
                        agent_id='a', workspace_id='w', user_id='u', payload_json='{}')
        self.export = Mock(return_value='mock-only.md')
        self.mods = {
            'eimemory.models.records': types.SimpleNamespace(RecordEnvelope=types.SimpleNamespace(from_dict=Mock(return_value=self.record))),
            'eimemory.storage.record_export': types.SimpleNamespace(export_record_markdown=self.export),
        }

    def connection(self, count=1, cold=0, rows=None, columns=None):
        c = Mock()
        c.execute.side_effect = [[(i, name) for i, name in enumerate(self.required if columns is None else columns)],
                                 Mock(fetchone=Mock(return_value=(count, cold))), [self.row] if rows is None else rows]
        return c

    def build(self, connection):
        with patch.dict(sys.modules, self.mods):
            return self.env['_build_into'](connection, Path('/inert/staging'), max_records=2)

    def test_invalid_limits_do_not_touch_paths(self):
        snapshot = Mock()
        for value in [0, -1, True, 1.0, None]:
            with self.assertRaisesRegex(ValueError, 'invalid_record_limit'):
                self.env['rebuild'](snapshot, Mock(), max_records=value)
        snapshot.absolute.assert_not_called()

    def test_preflight_branches(self):
        for c, error in [(self.connection(columns=[]), 'unsupported_records_schema'),
                         (self.connection(count=3), 'export_record_limit_exceeded'),
                         (self.connection(cold=1), 'cold_export_payload_requires_full_snapshot_reader')]:
            with self.assertRaisesRegex(ValueError, error):
                self.build(c)
        self.export.assert_not_called()

    def test_empty_and_counts(self):
        result = self.build(self.connection(count=0, rows=[]))
        self.assertEqual((result['source_record_count'], result['exported_count']), (0, 0))
        self.assertEqual(self.build(self.connection())['exported_count'], 1)
        self.export.return_value = None
        self.assertEqual(self.build(self.connection())['exported_count'], 0)

    def test_payload_type_and_callback_failures(self):
        self.row['payload_json'] = '[]'
        with self.assertRaisesRegex(ValueError, 'record_payload_not_object'):
            self.build(self.connection())
        self.row['payload_json'] = '{}'
        self.export.side_effect = OSError('stub failure')
        with self.assertRaisesRegex(OSError, 'stub failure'):
            self.build(self.connection())

    def test_field_comparison_branches_only(self):
        for key in ['record_id', 'kind', 'status', 'source_id', 'tenant_id', 'agent_id', 'workspace_id', 'user_id']:
            old = self.row[key]
            self.row[key] = 'different literal'
            with self.assertRaisesRegex(ValueError, 'record_projection_identity_mismatch'):
                self.build(self.connection())
            self.row[key] = old
        self.export.assert_not_called()

    def test_main_stub_success_and_failure(self):
        args = ['--offline-snapshot', '/inert/snapshot', '--new-output', '/inert/new']
        self.env['rebuild'] = Mock(return_value={'complete': True})
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(self.env['main'](args), 0)
            self.assertEqual(json.loads(out.getvalue()), {'complete': True})
        for exc in [ValueError('value'), OSError('os'), sqlite3.Error('db')]:
            self.env['sys'].stderr = io.StringIO()
            self.env['rebuild'].side_effect = exc
            self.assertEqual(self.env['main'](args), 2)
            self.assertEqual(json.loads(self.env['sys'].stderr.getvalue())['error_type'], type(exc).__name__)


if __name__ == '__main__':
    unittest.main()
