"""Offline regression: compile only reviewed functions, never import the ops module."""
import ast
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Mapping
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/ops/backfill_capability_v3.py'

ALLOWED = {'_begin_state', '_full_migration_complete', '_plan_matches',
           '_state_phase_stats', '_safe_int', '_canonical_json'}
TEXT_COLUMNS = ('migration_id status phase context_digest context_json updated_at '
                'backfill_plan_digest backfill_plan_json phase_stats_json cursor '
                'skipped_reasons_json source_watermark source_digest target_digest '
                'last_batch_digest last_batch_json last_error started_at finished_at').split()
INT_COLUMNS = ('rows_scanned rows_written rows_skipped batch_count source_total '
               'destination_total last_duration_ms restart_count').split()

def build_namespace(source):
    tree = ast.parse(source)
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ALLOWED]
    assert {node.name for node in selected} == ALLOWED
    namespace = {'Any': Any, 'Mapping': Mapping, 'json': json,
                 'CapabilityBackfillError': RuntimeError, '_utc_now': lambda: 'new-time',
                 '_BACKFILL_PHASES': (SimpleNamespace(name='audit_definition'),),
                 '_BACKFILL_PHASE_BY_NAME': {'audit_definition': object()}}
    def read_state(conn, *, migration_id):
        row = conn.execute('SELECT * FROM capability_v3_migration_state WHERE migration_id=?', (migration_id,)).fetchone()
        return dict(row) if row is not None else {'status': 'not_installed'}
    namespace['capability_v3_backfill_state'] = read_state
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<reviewed-functions-only>', 'exec'), namespace)
    return namespace

class CompletedStateTests(unittest.TestCase):
    source = SOURCE.read_text(encoding="utf-8")
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        fields = [name + " TEXT DEFAULT ''" + (' PRIMARY KEY' if name == 'migration_id' else '') for name in TEXT_COLUMNS]
        fields += [name + ' INTEGER DEFAULT 0' for name in INT_COLUMNS]
        self.conn.execute('CREATE TABLE capability_v3_migration_state (' + ','.join(fields) + ')')
        self.context = {'schema': 'test', 'runtime_scope': {}, 'capability_scope': 'global',
                        'context_migration_id': 'test-migration', 'context_digest': 'context'}
        self.plan = {'digest': 'current-plan'}
        self.ns = build_namespace(self.source)
    def tearDown(self):
        self.conn.close()
    def put(self, **updates):
        fields = dict(migration_id='test-migration', context_digest='context',
                      backfill_plan_digest='current-plan', status='completed', phase='completed',
                      phase_stats_json=json.dumps({'audit_definition': {'status': 'completed'}}),
                      finished_at='old-finished', rows_scanned=8, rows_written=3, cursor='last-cursor')
        fields.update(updates)
        self.conn.execute('INSERT INTO capability_v3_migration_state (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')', tuple(fields.values()))
        self.conn.commit()
    def begin(self):
        return self.ns['_begin_state'](self.conn, previous={'status': 'running'}, context=self.context, plan=self.plan)
    def test_fresh_completed_row_is_preserved(self):
        self.put()
        before = self.ns['capability_v3_backfill_state'](self.conn, migration_id='test-migration')
        result = self.begin()
        self.assertEqual(result, before)
        self.assertTrue(self.ns['_full_migration_complete'](result, self.plan))
        self.assertFalse(self.conn.in_transaction)
    def test_failed_row_still_resumes(self):
        self.put(status='failed', phase='audit_definition')
        result = self.begin()
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['cursor'], 'last-cursor')
        self.assertEqual(result['restart_count'], 1)
        self.assertFalse(self.conn.in_transaction)
    def test_obsolete_completed_row_still_resets(self):
        self.put(backfill_plan_digest='old-plan')
        result = self.begin()
        self.assertEqual(result['phase'], 'audit_definition')
        self.assertEqual(result['rows_scanned'], 0)
        self.assertEqual(result['cursor'], '')
        self.assertFalse(self.conn.in_transaction)
    def test_context_mismatch_still_rejects(self):
        self.put(context_digest='different')
        with self.assertRaisesRegex(RuntimeError, 'context does not match'):
            self.begin()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.ns['capability_v3_backfill_state'](self.conn, migration_id='test-migration')['status'], 'completed')

if __name__ == '__main__':
    unittest.main(verbosity=2)
