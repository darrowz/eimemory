"""Selected local bodies only; every filesystem/lock effect is a pure fake."""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PosixPath
import re
from types import SimpleNamespace
import unittest

MODE = 'checkout'
SOURCE = Path(__file__).resolve().parents[1] / 'deploy/storage_release_transaction.py'
source = SOURCE.read_text()
FUNCTIONS = {
    '_absolute_path', '_nonblank', '_validated_transaction',
    'update_storage_release_transaction', 'clear_storage_release_transaction',
    '_clear_tombstone', '_recovery_blocker', '_exclusive_lock_is_held',
    'guard_allows_start', 'classify_storage_release_reconcile',
}
CONSTANTS = {'SCHEMA', '_COMMIT_RE', '_DIGEST_RE', '_UNIT_RE', '_PHASES', '_ROLLBACK_RESTORED_PHASE_ORDER'}
body = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
for node in ast.parse(source).body:
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
        body.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == 'StorageReleaseTransactionError':
        body.append(node)
    elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in CONSTANTS for t in node.targets):
        body.append(node)
module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))

class NoIOPath(PosixPath):
    def is_symlink(self):
        return False
    def is_dir(self):
        return True
    def resolve(self, strict=False):
        return self

class LocalContractTests(unittest.TestCase):
    def setUp(self):
        self.effects = []
        self.payload = {
            'schema': 'storage_release_transaction.v1', 'status': 'in_progress',
            'phase': 'writers_captured', 'prior_commit': '1' * 40,
            'candidate_commit': '2' * 40, 'attempt_id': '123',
            'current_link': '/synthetic/current', 'snapshot_dir': '/synthetic/snapshot',
            'snapshot_manifest_sha256': '', 'storage_destructive': False,
            'active_writer_units': [], 'vacuum_backup_path': '',
        }
        @contextmanager
        def fake_lock(marker):
            yield None
        def fake_load(marker, **kwargs):
            return self.ns['_validated_transaction'](dict(self.payload))
        def fake_open(path, flags):
            # Model Python's rejected path-argument branch without filesystem IO.
            self.effects.append(('open-argument', str(path)))
            if '\0' in str(path):
                raise ValueError('embedded null byte')
            raise AssertionError('unexpected filesystem call: ' + str(path))
        self.ns = {
            '__name__': '__main__', 'Path': NoIOPath, 'Any': object,
            're': re, 'datetime': datetime, 'timezone': timezone,
            '_marker_lock': fake_lock, '_load_storage_release_transaction_unlocked': fake_load,
            '_marker_entry_exists': lambda path, **kw: path.name == 'marker',
            '_atomic_write_json': lambda *args, **kw: self.effects.append(('write', args[1])),
            '_replace_marker_entry': lambda *args, **kw: self.effects.append(('replace',)),
            '_sync_marker_parent': lambda *args, **kw: self.effects.append(('sync',)),
            '_create_recovery_blocker': lambda *args, **kw: self.effects.append(('blocker',)),
            '_unlink_marker_entry': lambda *args, **kw: self.effects.append(('unlink',)),
            'os': SimpleNamespace(name='posix', O_RDONLY=0, O_NOFOLLOW=0, open=fake_open),
        }
        exec(compile(module, 'selected-local-' + MODE, 'exec'), self.ns)
        self.Error = self.ns['StorageReleaseTransactionError']

    def test_numeric_attempt_survives_update_expected_string(self):
        self.payload['attempt_id'] = 123
        out = self.ns['update_storage_release_transaction']('/synthetic/marker', expected_attempt_id='123', phase='writers_stopped')
        self.assertEqual(out['attempt_id'], '123')
        self.assertEqual(out['phase'], 'writers_stopped')
        self.assertEqual(len(self.effects), 1)

    def test_numeric_attempt_survives_clear_expected_string(self):
        self.payload['attempt_id'] = 123
        self.ns['clear_storage_release_transaction']('/synthetic/marker', expected_attempt_id='123')
        self.assertEqual([item[0] for item in self.effects], ['replace', 'sync', 'blocker', 'unlink', 'sync', 'unlink', 'sync'])

    def test_numeric_prior_commit_matches_current_string(self):
        self.payload['prior_commit'] = int('1' * 40)
        self.assertEqual(self.ns['classify_storage_release_reconcile'](self.payload, current_commit='1' * 40, migrations_complete=False), 'clear_prior')

    def test_numeric_candidate_commit_matches_current_string(self):
        self.payload['candidate_commit'] = int('2' * 40)
        self.payload['phase'] = 'candidate_validated'
        self.assertEqual(self.ns['classify_storage_release_reconcile'](self.payload, current_commit='2' * 40, migrations_complete=False), 'finalize_candidate')

    def test_numeric_prior_commit_matches_restored_phase(self):
        self.payload['prior_commit'] = int('1' * 40)
        self.payload['phase'] = 'rollback_validated'
        self.assertEqual(self.ns['classify_storage_release_reconcile'](self.payload, current_commit='1' * 40, migrations_complete=False), 'finalize_rollback')

    def test_string_fields_remain_exact_and_wrong_attempt_still_rejects(self):
        self.payload['attempt_id'] = ' 123 '
        self.payload['prior_commit'] = 'A' * 40
        out = self.ns['_validated_transaction'](dict(self.payload))
        self.assertEqual(out['attempt_id'], ' 123 ')
        self.assertEqual(out['prior_commit'], 'A' * 40)
        with self.assertRaisesRegex(self.Error, 'attempt mismatch'):
            self.ns['update_storage_release_transaction']('/synthetic/marker', expected_attempt_id='123', phase='writers_stopped')
        self.assertEqual(self.effects, [])

    def test_invalid_commit_and_blank_attempt_still_reject(self):
        for field, value in [('prior_commit', 123), ('candidate_commit', 'g' * 40), ('attempt_id', 0), ('attempt_id', 'a\0b')]:
            with self.subTest(field=field, value=value):
                payload = dict(self.payload)
                payload[field] = value
                with self.assertRaises(self.Error):
                    self.ns['_validated_transaction'](payload)

    def test_all_path_fields_reject_nul_during_validation(self):
        for field in ['current_link', 'snapshot_dir', 'deployment_lock_path', 'candidate_validation_lock_path', 'vacuum_backup_path']:
            with self.subTest(field=field):
                payload = dict(self.payload)
                payload[field] = '/synthetic/a\0b'
                with self.assertRaisesRegex(self.Error, 'absolute normalized path'):
                    self.ns['_validated_transaction'](payload)
        self.assertEqual(self.effects, [])

    def test_guard_rejects_nul_lock_before_os_argument_boundary(self):
        self.payload['phase'] = 'candidate_validating'
        self.payload['deployment_lock_path'] = '/synthetic/lock\0bad'
        self.assertIs(self.ns['guard_allows_start']('/synthetic/marker'), False)
        self.assertEqual(self.effects, [])

    def test_valid_absolute_path_and_lock_defaults_unchanged(self):
        out = self.ns['_validated_transaction'](dict(self.payload))
        self.assertEqual(out['deployment_lock_path'], '/synthetic/.storage-release-install.lock')
        self.assertEqual(out['candidate_validation_lock_path'], '/synthetic/.candidate-validation.lock')
        self.assertEqual(self.ns['_absolute_path']('/synthetic/a b', label='path'), '/synthetic/a b')
        for bad in ['relative', '/synthetic/../path']:
            with self.assertRaises(self.Error):
                self.ns['_absolute_path'](bad, label='path')

if __name__ == '__main__':
    print('MODE=' + MODE + '; source_sha256=' + sha256(source.encode()).hexdigest(), flush=True)
    unittest.main(verbosity=2)
