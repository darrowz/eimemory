"""Local metadata regressions; AST-extracted functions with inert dependencies only.

Run this file directly with Python, avoiding project imports and pytest hooks.
BACKUP_METADATA_SOURCE can point at a baseline source for differential checks.
No SQL, archive, CLI, backup, deletion, chmod or directory creation is performed.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
import io
import json
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest


SOURCE = Path(os.environ.get("BACKUP_METADATA_SOURCE", str(
    Path(__file__).resolve().parents[1] / "deploy" / "eimemory_backup.py"
)))


def extracted(name, **injected):
    """Compile exactly one definition, never the project module or imports."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ast.fix_missing_locations(isolated)
    namespace = dict(injected)
    exec(compile(isolated, str(SOURCE), "exec"), namespace)
    return namespace[name]


class InertPath(PurePosixPath):
    """All operations used by exercised code are in-memory or explicit failures."""
    members = ()
    resolve_error = None

    def mkdir(self, *args, **kwargs):
        return None

    def exists(self):
        return True  # Force run_backup to return before any helper is called.

    def resolve(self):
        if self.resolve_error is not None:
            raise self.resolve_error
        return self

    def rglob(self, pattern):
        return iter(self.members)

    def is_file(self):
        return True

    def stat(self):
        return SimpleNamespace(st_size=7)


def forbidden(*args, **kwargs):
    raise AssertionError("A real operation or unapproved helper was reached")


class BackupLocalMetadataTests(unittest.TestCase):
    def setUp(self):
        InertPath.members = ()
        InertPath.resolve_error = None

    def timestamp_report(self, now=None, clock=datetime):
        run = extracted(
            "run_backup", datetime=clock, timezone=timezone,
            os=SimpleNamespace(chmod=lambda *a: None),
            _sqlite_sources=forbidden, _backup_sqlite=forbidden,
            _run_cli=forbidden, _copy_state_files=forbidden,
            _archive_config=forbidden, _file_inventory=forbidden,
            _prune=forbidden, shutil=SimpleNamespace(rmtree=forbidden),
        )
        return run(root=InertPath('/root'), config_dir=InertPath('/config'),
                   backup_root=InertPath('/backups'), eimemory_bin='inert', keep=5, now=now)

    def test_inventory_keeps_nested_same_name(self):
        root = InertPath('/set')
        InertPath.members = tuple(InertPath(p) for p in (
            '/set/backup-set.json', '/set/state/root/backup-set.json',
            '/set/state/state/backup-set.json', '/set/state/ordinary.json'))
        digested = []
        def digest(path):
            digested.append(str(path))
            return 'inert-digest'
        inventory = extracted('_file_inventory', _sha256=digest)(root)
        self.assertEqual([v['path'] for v in inventory], [
            'state/ordinary.json', 'state/root/backup-set.json',
            'state/state/backup-set.json'])
        self.assertEqual(sum(v['bytes'] for v in inventory), 21)
        self.assertEqual(len(digested), 3)
        self.assertNotIn('/set/backup-set.json', digested)

    def test_aware_positive_offset_crosses_previous_year(self):
        report = self.timestamp_report(datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(report['path'], '/backups/20251231T170000Z')

    def test_aware_negative_offset_crosses_next_year(self):
        report = self.timestamp_report(datetime(2025, 12, 31, 23, tzinfo=timezone(-timedelta(hours=5))))
        self.assertEqual(report['path'], '/backups/20260101T040000Z')

    def test_naive_preserves_literal_utc_name(self):
        report = self.timestamp_report(datetime(2026, 1, 1, 1, 2, 3))
        self.assertEqual(report['path'], '/backups/20260101T010203Z')

    def test_naive_tzinfo_without_offset_uses_utc(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None
            def dst(self, dt):
                return None
            def tzname(self, dt):
                return None
        class NoLocalFallback(datetime):
            def astimezone(self, tz=None):
                if self.utcoffset() is None:
                    raise AssertionError('Naive input would consult host local timezone')
                return super().astimezone(tz)
        now = NoLocalFallback(2026, 1, 1, 1, 2, 3, tzinfo=NoOffset())
        self.assertEqual(self.timestamp_report(now)['path'], '/backups/20260101T010203Z')

    def test_utc_aware_unchanged(self):
        report = self.timestamp_report(datetime(2026, 1, 1, 1, 2, 3, tzinfo=timezone.utc))
        self.assertEqual(report['path'], '/backups/20260101T010203Z')

    def test_default_requests_utc(self):
        calls = []
        class Clock:
            @staticmethod
            def now(tz):
                calls.append(tz)
                return datetime(2026, 1, 1, 1, 2, 3, tzinfo=tz)
        self.assertEqual(self.timestamp_report(clock=Clock)['path'], '/backups/20260101T010203Z')
        self.assertEqual(calls, [timezone.utc])

    def cli(self, run):
        return extracted('main', argparse=argparse, __doc__='Inert backup test',
                         os=SimpleNamespace(environ={}, umask=lambda *a: None),
                         Path=InertPath, run_backup=run, json=json)

    def capture_cli(self, run, argv=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli(run)([] if argv is None else argv)
        return code, output.getvalue()

    def test_success_report_and_arguments_preserved(self):
        calls = []
        def run(**kwargs):
            calls.append(kwargs)
            return {'ok': True, 'path': '/inert', 'pruned': []}
        code, output = self.capture_cli(run, ['--root', '/r', '--config-dir', '/c',
            '--backup-root', '/b', '--eimemory-bin', 'fake', '--keep', '100'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {'ok': True, 'path': '/inert', 'pruned': []})
        self.assertEqual(calls, [dict(root=InertPath('/r'), config_dir=InertPath('/c'),
                                   backup_root=InertPath('/b'), eimemory_bin='fake', keep=60)])

    def test_existing_failure_report_preserved(self):
        report = {'ok': False, 'reason': 'backup_set_exists', 'path': '/inert'}
        code, output = self.capture_cli(lambda **kwargs: report)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), report)

    def test_exception_report_omits_sensitive_text(self):
        def run(**kwargs):
            raise OSError('sensitive-config=do-not-print' * 10000)
        code, output = self.capture_cli(run)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), dict(ok=False, reason='backup_failed', error_type='OSError'))
        self.assertNotIn('sensitive', output)
        self.assertLess(len(output), 256)

    def test_path_preparation_exception_is_json(self):
        InertPath.resolve_error = ValueError('private-path-do-not-print')
        code, output = self.capture_cli(forbidden)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), dict(ok=False, reason='backup_failed', error_type='ValueError'))
        self.assertNotIn('private', output)

    def test_long_error_class_name_is_bounded(self):
        error = type('X' * 1000, (Exception,), {})
        def run(**kwargs):
            raise error('secret')
        code, output = self.capture_cli(run)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error_type'], 'X' * 80)
        self.assertLess(len(output), 256)

    def test_cancellation_exceptions_propagate(self):
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error.__name__):
                def run(**kwargs):
                    raise error('cancel')
                with self.assertRaises(error):
                    self.capture_cli(run)


if __name__ == '__main__':
    unittest.main()
