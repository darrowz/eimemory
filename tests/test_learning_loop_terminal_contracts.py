"""Synthetic-only loop lifecycle regressions: no application module imports.

Run with: python -I tests/test_learning_loop_terminal_contracts.py
The reviewed state helpers, scheduler lease reader, and cycle finalization tail
are AST-extracted into an isolated standard-library fixture namespace. No real
cycle, Runtime, provider, evaluator, environment helper, or real data is loaded.
"""
from __future__ import annotations

import ast
import builtins
import copy
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
CLOCK = [NOW]


def now_iso():
    return CLOCK[0].isoformat(timespec='seconds').replace('+00:00', 'Z')


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK[0] if tz else CLOCK[0].replace(tzinfo=None)


@dataclass
class ScopeFixture:
    tenant_id: str = 'default'
    agent_id: str = 'test-agent'
    workspace_id: str = ''
    user_id: str = ''

    @classmethod
    def from_dict(cls, value):
        return cls(**(value or {}))


class RecordFixture:
    serial = 0

    def __init__(self, *, kind='learning_loop', status='running', title='', summary='', content=None, meta=None, scope=None, record_id=None, source_id='default', **unused):
        type(self).serial += 1
        self.record_id = record_id or f'learning_loop_{type(self).serial}'
        self.kind, self.status = kind, status
        self.title, self.summary = title, summary
        self.content = dict(content or {'steps': []})
        self.meta = dict(meta or {'status': status, 'loop_id': self.record_id})
        self.scope = scope or ScopeFixture()
        self.source_id = source_id
        self.time = SimpleNamespace(created_at=now_iso(), updated_at=now_iso())

    @classmethod
    def create(cls, **kwargs):
        return cls(**kwargs)

    def touch(self):
        self.time.updated_at = now_iso()


class StoreFixture:
    def __init__(self, records=()):
        self.records = {r.record_id: copy.deepcopy(r) for r in records}
        self.lock = RLock()
        self.writes = []
        self.attempts = []
        self.fail_rewrite = None
        self.after_list = None

    @contextmanager
    def locked(self):
        with self.lock:
            yield self

    def get_by_id(self, record_id, *, scope=None):
        with self.lock:
            record = self.records.get(record_id)
            if record is not None and scope is not None and record.scope != scope:
                return None
            return copy.deepcopy(record)

    def append(self, record):
        with self.lock:
            self.records[record.record_id] = copy.deepcopy(record)
            self.writes.append(copy.deepcopy(record))
            return record

    def rewrite(self, record):
        with self.lock:
            self.attempts.append(copy.deepcopy(record))
            if self.fail_rewrite and self.fail_rewrite(record, len(self.attempts)):
                raise OSError('synthetic store write failed')
            if record.record_id not in self.records:
                raise ValueError('synthetic record missing')
            self.records[record.record_id] = copy.deepcopy(record)
            self.writes.append(copy.deepcopy(record))
            return record

    def get_by_idempotency_key(self, *, kinds, scope, idempotency_key):
        for record in self.records.values():
            if record.kind in kinds and record.scope == scope and record.meta.get('idempotency_key') == idempotency_key:
                return copy.deepcopy(record)
        return None

    def list_records(self, *, kinds=None, scope=None, limit=100, offset=0):
        records = [copy.deepcopy(r) for r in self.records.values() if (not kinds or r.kind in kinds) and (scope is None or r.scope == scope)]
        records.sort(key=lambda r: (r.time.updated_at, r.record_id), reverse=True)
        if self.after_list:
            callback, self.after_list = self.after_list, None
            callback()
        return records[offset:offset + limit]


class PartitionedStoreFixture(StoreFixture):
    """Same-ID records coexist; ordinary lookup admits shared-user fallback."""
    @staticmethod
    def key(record):
        scope = record.scope
        return (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, record.source_id, record.record_id)

    def __init__(self, records):
        super().__init__()
        self.records = {self.key(record): copy.deepcopy(record) for record in records}

    def get_by_id(self, record_id, *, scope=None):
        rows = [record for record in self.records.values() if record.record_id == record_id and (
            scope is None or (record.scope.tenant_id == scope.tenant_id and record.scope.agent_id == scope.agent_id
                              and record.scope.workspace_id == scope.workspace_id
                              and record.scope.user_id in ({scope.user_id, ''} if scope.user_id else {''})))]
        rows.sort(key=lambda record: record.time.updated_at, reverse=True)
        return copy.deepcopy(rows[0]) if rows else None

    def get_by_exact_ref(self, record_id, *, scope, source_id):
        key = (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, source_id, record_id)
        return copy.deepcopy(self.records.get(key))

    def rewrite(self, record):
        self.attempts.append(copy.deepcopy(record))
        self.records[self.key(record)] = copy.deepcopy(record)
        self.writes.append(copy.deepcopy(record))
        return record


def extract_functions(path, names, namespace, constants=()):
    tree = ast.parse((ROOT / path).read_text(), filename=path)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            selected.append(node)
    found = {node.name for node in selected if isinstance(node, ast.FunctionDef)}
    if found != set(names):
        raise AssertionError(f'missing helper(s): {set(names) - found}')
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, 'exec', flags=__import__('__future__').annotations.compiler_flag), namespace)


def load_helpers():
    ns = {'__builtins__': dict(vars(builtins)), 'datetime': FrozenDatetime, 'timezone': timezone, 'now_iso': now_iso,
          'sha256': sha256, 'asdict': asdict, 'nullcontext': nullcontext, 'ScopeRef': ScopeFixture,
          'RecordEnvelope': RecordFixture, 'generate_record_id': lambda kind: f'{kind}_{RecordFixture.serial + 1}'}
    extract_functions('eimemory/governance/learning/learning_state.py', {
        'scope_payload', 'stable_semantic_key', 'learning_idempotency_key', 'start_learning_loop',
        'active_learning_loops', 'recover_stale_learning_loops', 'mark_step', 'complete_learning_loop',
        '_finalize_learning_loop_record', 'find_record_by_idempotency', '_resolve_loop', '_learning_store_lock', '_record_age_seconds',
    }, ns, {'AUTONOMOUS_LEARNING_SCHEMA_VERSION', 'ACTIVE_LOOP_STATUSES', 'TERMINAL_LOOP_STATUSES', 'DEFAULT_STALE_LOOP_SECONDS'})

    def isolated_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'eimemory.governance.learning.learning_state':
            return SimpleNamespace(active_learning_loops=ns['active_learning_loops'])
        raise AssertionError(f'application import forbidden: {name}')
    ns['__builtins__']['__import__'] = isolated_import
    extract_functions('eimemory/scheduler/jobs.py', {'_reread_autonomous_learning_lease'}, ns)

    # Extract only the production terminal tail plus its exact outer exception
    # handler. Every preceding task/evaluator/provider operation is excluded.
    path = 'eimemory/governance/learning/autonomous_learning.py'
    tree = ast.parse((ROOT / path).read_text(), filename=path)
    cycle = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run_autonomous_learning_cycle')
    outer = next(node for node in cycle.body if isinstance(node, ast.Try))
    classify_index = next(index for index, node in enumerate(outer.body)
                          if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'update'
                          and ast.unparse(node.value.func.value) == 'result')
    tail = copy.deepcopy(outer.body[classify_index:])
    if len(tail) != 3 or 'classify_autonomous_learning_activity' not in ast.unparse(tail[0]) or 'complete_learning_loop' not in ast.unparse(tail[1]) or not isinstance(tail[2], ast.Return):
        raise AssertionError('normal finalization must follow classification immediately before return')
    terminal_calls = [node for node in ast.walk(outer) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name) and node.func.id == 'complete_learning_loop']
    if len(terminal_calls) != 2:
        raise AssertionError('expected one normal and one exception finalization')
    synthetic_raise = ast.parse('if error is not None:\n    raise error').body[0]
    harness = ast.FunctionDef(name='run_terminal_tail', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=name) for name in ('runtime', 'loop', 'result', 'error', 'candidate_id')], kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=[ast.Try(body=[synthetic_raise, *tail], handlers=copy.deepcopy(outer.handlers), orelse=[], finalbody=[])], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[]))
    exec(compile(module, path + ':terminal-tail-only', 'exec', flags=__import__('__future__').annotations.compiler_flag), ns)
    ns['classify_autonomous_learning_activity'] = lambda result: {'activity_class': 'fixture'}
    return ns


class LearningLoopTerminalContracts(unittest.TestCase):
    def setUp(self):
        CLOCK[0] = NOW
        self.ns = load_helpers()
        self.loop = RecordFixture()
        self.store = StoreFixture([self.loop])
        self.runtime = SimpleNamespace(store=self.store)

    def call(self, name, *args, **kwargs):
        return self.ns[name](*args, **kwargs)

    def persisted(self):
        return self.store.get_by_id(self.loop.record_id)

    def test_all_step_outcomes_preserve_active_loop_and_evidence(self):
        for status in ('completed', 'blocked', 'failed', 'skipped', 'running'):
            with self.subTest(status=status):
                result = self.call('mark_step', self.runtime, self.loop, step_name=status, status=status, error='reason', record_ids=['evidence'], metrics={'count': 2})
                self.assertEqual(result.status, 'running')
                self.assertEqual(result.meta['status'], 'running')
                self.assertEqual(result.content['steps'][-1]['status'], status)
                self.assertEqual(result.content['steps'][-1]['error'], 'reason')
                self.assertEqual(result.content['steps'][-1]['record_ids'], ['evidence'])
                self.assertEqual(result.meta['last_step_status'], status)
        self.assertEqual(len(self.call('active_learning_loops', self.runtime)), 1)

    def test_stale_object_progress_accumulates_instead_of_overwriting_steps(self):
        self.call('mark_step', self.runtime, self.loop, step_name='observe', status='completed')
        self.call('mark_step', self.runtime, self.loop, step_name='research', status='completed')
        self.assertEqual([item['step_name'] for item in self.persisted().content['steps']], ['observe', 'research'])

    def test_same_step_upsert_keeps_existing_contract(self):
        self.call('mark_step', self.runtime, self.loop, step_name='observe', status='running', record_ids=['a'])
        result = self.call('mark_step', self.runtime, self.loop, step_name='observe', status='completed', record_ids=['a', 'b'])
        self.assertEqual(len(result.content['steps']), 1)
        self.assertEqual(result.content['steps'][0]['record_ids'], ['a', 'b'])

    def test_scheduler_reports_busy_after_completed_or_blocked_step(self):
        for status in ('completed', 'blocked'):
            self.call('mark_step', self.runtime, self.loop, step_name=status, status=status)
            lease = self.call('_reread_autonomous_learning_lease', self.runtime, scope=asdict(self.loop.scope), report={})
            self.assertEqual((lease['state'], lease['active_loop_count']), ('busy', 1))
            self.assertEqual(lease['side_effects'], 'unknown')
        self.call('complete_learning_loop', self.runtime, self.loop)
        lease = self.call('_reread_autonomous_learning_lease', self.runtime, scope=asdict(self.loop.scope), report={})
        self.assertEqual((lease['state'], lease['active_loop_count']), ('idle', 0))

    def test_start_guard_still_blocks_after_step_completion(self):
        self.call('mark_step', self.runtime, self.loop, step_name='observe', status='completed')
        with self.assertRaisesRegex(RuntimeError, 'active learning loop already exists'):
            self.call('start_learning_loop', self.runtime, scope=self.loop.scope)

    def test_late_stale_step_is_noop_after_each_terminal_status(self):
        for status in ('completed', 'failed', 'blocked'):
            with self.subTest(status=status):
                loop = RecordFixture()
                store = StoreFixture([loop])
                runtime = SimpleNamespace(store=store)
                self.call('complete_learning_loop', runtime, loop, status=status, summary='final')
                saved = copy.deepcopy(store.records[loop.record_id].__dict__)
                writes = len(store.writes)
                CLOCK[0] += timedelta(hours=1)
                result = self.call('mark_step', runtime, loop, step_name='late', status='running')
                self.assertEqual(result.status, status)
                self.assertEqual(store.records[loop.record_id].__dict__, saved)
                self.assertEqual(len(store.writes), writes)

    def test_repeat_finalize_is_idempotent_and_keeps_first_timestamp_summary(self):
        first = self.call('complete_learning_loop', self.runtime, self.loop, summary='first')
        CLOCK[0] += timedelta(days=1)
        second = self.call('complete_learning_loop', self.runtime, self.loop, summary='late replacement')
        self.assertEqual(second.__dict__, first.__dict__)
        self.assertEqual(len(self.store.writes), 1)

    def test_conflicting_finalize_rejected_without_rewriting_terminal(self):
        self.call('complete_learning_loop', self.runtime, self.loop, status='completed')
        with self.assertRaisesRegex(ValueError, 'already finalized as completed'):
            self.call('complete_learning_loop', self.runtime, self.loop, status='failed')
        self.assertEqual(self.persisted().status, 'completed')
        self.assertEqual(len(self.store.writes), 1)

    def test_invalid_terminal_status_does_not_write(self):
        with self.assertRaisesRegex(ValueError, 'invalid terminal'):
            self.call('complete_learning_loop', self.runtime, self.loop, status='cancelled')
        self.assertEqual(self.store.writes, [])

    def test_normal_tail_finalizes_after_classification_and_preserves_result(self):
        def classify(result):
            self.assertEqual(self.persisted().status, 'running')
            return {'activity_class': 'fixture'}
        self.ns['classify_autonomous_learning_activity'] = classify
        result = {'ok': True, 'candidate_ids': ['fixture']}
        returned = self.call('run_terminal_tail', self.runtime, self.loop, result, None, 'fixture')
        self.assertIs(returned, result)
        self.assertEqual(returned, {'ok': True, 'candidate_ids': ['fixture'], 'activity_class': 'fixture'})
        self.assertEqual(self.persisted().status, 'completed')
        self.assertEqual([record.status for record in self.store.writes], ['completed'])

    def test_classification_failure_never_persists_completed(self):
        original = ValueError('synthetic classification failure')
        def classify(result):
            raise original
        self.ns['classify_autonomous_learning_activity'] = classify
        with self.assertRaises(ValueError) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, None, 'fixture')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'failed')
        self.assertNotIn('completed', [record.status for record in self.store.writes])
        self.assertEqual(self.persisted().content['steps'][0]['error'], str(original))

    def test_failed_step_write_still_attempts_failed_finalize_and_reraises_original(self):
        self.store.fail_rewrite = lambda record, count: count == 1
        original = RuntimeError('synthetic original task error')
        with self.assertRaises(RuntimeError) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'failed')
        self.assertEqual(len(self.store.attempts), 2)
        self.assertIn('failed-step persistence', original.__notes__[0])

    def test_terminal_write_failure_keeps_persisted_active_and_original_error(self):
        self.store.fail_rewrite = lambda record, count: record.status == 'failed'
        original = RuntimeError('original')
        with self.assertRaises(RuntimeError) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'running')
        self.assertEqual(self.persisted().content['steps'][0]['status'], 'failed')
        self.assertIn('finalization also failed', original.__notes__[0])

    def test_both_cleanup_writes_fail_without_swallowing_original(self):
        self.store.fail_rewrite = lambda record, count: True
        original = RuntimeError('original')
        with self.assertRaises(RuntimeError) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'running')
        self.assertEqual(len(original.__notes__), 2)

    def test_cancellation_preserves_existing_baseexception_behavior(self):
        original = KeyboardInterrupt('synthetic cancellation')
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.persisted().status, 'running')
        CLOCK[0] += timedelta(hours=6)
        recovered = self.call('recover_stale_learning_loops', self.runtime, reason='stale_after_cancel')
        self.assertEqual(recovered[0].status, 'failed')
        self.assertEqual(recovered[0].meta['stale_recovered_reason'], 'stale_after_cancel')

    def test_stale_recovery_threshold_is_inclusive_and_keeps_forensics(self):
        CLOCK[0] += timedelta(seconds=21599)
        self.assertEqual(self.call('recover_stale_learning_loops', self.runtime), [])
        CLOCK[0] += timedelta(seconds=1)
        recovered = self.call('recover_stale_learning_loops', self.runtime, reason='fixture_stale')
        self.assertEqual(len(recovered), 1)
        final = recovered[0]
        self.assertEqual(final.status, 'failed')
        self.assertEqual(final.meta['previous_status'], 'running')
        self.assertEqual(final.meta['stale_age_seconds'], 21600)
        self.assertEqual(final.content['steps'][-1]['error'], 'fixture_stale')
        self.assertEqual(final.meta['finished_at'], final.content['finished_at'])
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual(self.call('recover_stale_learning_loops', self.runtime), [])

    def test_stale_recovery_rereads_freshly_touched_candidate(self):
        CLOCK[0] += timedelta(hours=7)
        def refresh():
            record = self.store.records[self.loop.record_id]
            record.time.updated_at = now_iso()
        self.store.after_list = refresh
        self.assertEqual(self.call('recover_stale_learning_loops', self.runtime), [])
        self.assertEqual(self.persisted().status, 'running')

    def test_stale_recovery_does_not_override_concurrently_finalized_candidate(self):
        CLOCK[0] += timedelta(hours=7)
        self.store.after_list = lambda: self.call('complete_learning_loop', self.runtime, self.loop, status='completed')
        self.assertEqual(self.call('recover_stale_learning_loops', self.runtime), [])
        self.assertEqual(self.persisted().status, 'completed')

    def test_existing_naive_invalid_future_age_behavior_preserved(self):
        for timestamp, expected in [('2026-10-02T19:00:00', 3600.0), ('invalid', 0.0), ('2026-10-03T00:00:00Z', 0.0)]:
            with self.subTest(timestamp=timestamp):
                record = RecordFixture()
                record.time.updated_at = timestamp
                self.assertEqual(self.call('_record_age_seconds', record, now=NOW), expected)

    def test_lightweight_store_without_lock_facade_stays_compatible(self):
        self.store.locked = None
        result = self.call('mark_step', self.runtime, self.loop.record_id, step_name='observe', status='completed')
        self.assertEqual(result.status, 'running')
        self.assertEqual(self.call('complete_learning_loop', self.runtime, self.loop.record_id).status, 'completed')

    def test_personal_loop_finalize_never_targets_newer_shared_same_id(self):
        personal = RecordFixture(record_id='same', scope=ScopeFixture(user_id='person'))
        shared = RecordFixture(record_id='same', scope=ScopeFixture(user_id=''))
        shared.time.updated_at = '2026-10-02T21:00:00Z'
        store = PartitionedStoreFixture([personal, shared])
        self.assertEqual(store.get_by_id('same', scope=personal.scope).scope.user_id, '')
        self.call('complete_learning_loop', SimpleNamespace(store=store), personal, status='failed')
        self.assertEqual(store.get_by_exact_ref('same', scope=personal.scope, source_id='default').status, 'failed')
        self.assertEqual(store.get_by_exact_ref('same', scope=shared.scope, source_id='default').status, 'running')

    def test_source_partition_is_part_of_authoritative_loop_identity(self):
        original = RecordFixture(record_id='same', source_id='original')
        foreign = RecordFixture(record_id='same', source_id='foreign')
        foreign.time.updated_at = '2026-10-02T21:00:00Z'
        store = PartitionedStoreFixture([original, foreign])
        self.call('mark_step', SimpleNamespace(store=store), original, step_name='observe', status='completed')
        self.assertEqual(len(store.get_by_exact_ref('same', scope=original.scope, source_id='original').content['steps']), 1)
        self.assertEqual(store.get_by_exact_ref('same', scope=foreign.scope, source_id='foreign').content['steps'], [])

    def test_missing_exact_identity_never_falls_back_to_shared(self):
        personal = RecordFixture(record_id='same', scope=ScopeFixture(user_id='person'))
        shared = RecordFixture(record_id='same', scope=ScopeFixture(user_id=''))
        store = PartitionedStoreFixture([shared])
        with self.assertRaisesRegex(ValueError, 'learning loop not found'):
            self.call('complete_learning_loop', SimpleNamespace(store=store), personal, status='failed')
        self.assertEqual(store.writes, [])

    def test_lightweight_lookup_fallback_rejects_shared_scope(self):
        personal = RecordFixture(record_id='same', scope=ScopeFixture(user_id='person'))
        shared = RecordFixture(record_id='same', scope=ScopeFixture(user_id=''))
        shared.time.updated_at = '2026-10-02T21:00:00Z'
        store = PartitionedStoreFixture([personal, shared])
        store.get_by_exact_ref = None
        with self.assertRaisesRegex(ValueError, 'learning loop identity mismatch'):
            self.call('mark_step', SimpleNamespace(store=store), personal, step_name='observe', status='completed')
        self.assertEqual(store.writes, [])

    def test_resolved_identity_checks_record_scope_source_and_kind(self):
        for field, value in [('record_id', 'wrong'), ('scope', ScopeFixture(agent_id='alias-agent')),
                             ('source_id', 'other-source'), ('kind', 'world_signal')]:
            with self.subTest(field=field):
                foreign = copy.deepcopy(self.loop)
                setattr(foreign, field, value)
                self.store.get_by_exact_ref = lambda *args, **kwargs: copy.deepcopy(foreign)
                with self.assertRaises(ValueError):
                    self.call('complete_learning_loop', self.runtime, self.loop, status='failed')
                self.assertEqual(self.store.writes, [])

    def test_broken_exception_notes_do_not_skip_failed_finalize(self):
        self.store.fail_rewrite = lambda record, count: count == 1
        original = RuntimeError('original')
        original.__notes__ = 'not a list'
        with self.assertRaises(RuntimeError) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'failed')
        self.assertEqual(len(self.store.attempts), 2)

    def test_broken_exception_notes_and_cleanup_failures_keep_original_trace(self):
        self.store.fail_rewrite = lambda record, count: True
        def original_failure_site():
            raise RuntimeError('original failure site')
        try:
            original_failure_site()
        except RuntimeError as original:
            original.__notes__ = 'not a list'
            try:
                self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
            except RuntimeError as raised:
                self.assertIs(raised, original)
                frames = []
                trace = raised.__traceback__
                while trace:
                    frames.append(trace.tb_frame.f_code.co_name)
                    trace = trace.tb_next
                self.assertIn('original_failure_site', frames)
            else:
                self.fail('original exception was swallowed')
        self.assertEqual(self.persisted().status, 'running')
        self.assertEqual(len(self.store.attempts), 2)

    def test_custom_add_note_override_is_not_called_during_cleanup(self):
        class CustomFailure(RuntimeError):
            def add_note(self, note):
                raise TypeError('untrusted annotation override')
        self.store.fail_rewrite = lambda record, count: count == 1
        original = CustomFailure('original')
        with self.assertRaises(CustomFailure) as caught:
            self.call('run_terminal_tail', self.runtime, self.loop, {}, original, '')
        self.assertIs(caught.exception, original)
        self.assertEqual(self.persisted().status, 'failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
