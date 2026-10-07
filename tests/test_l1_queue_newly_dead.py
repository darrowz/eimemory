"""Portable, stdlib-only ordinary counting regression; no project imports or I/O queue."""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(os.environ.get('L1_QUEUE_SOURCE', Path(__file__).resolve().parents[1] / 'eimemory/knowledge/l1_queue.py'))
METHODS = {'drain', 'drain_report', '_drain_owned', '_recover_interrupted',
           'dead_count', 'pending_count', '_claim', '_complete', '_fail'}


def extract_types():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))
    errors = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'L1QueueStateError']
    queues = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'L1ExtractQueue']
    if len(errors) != 1 or len(queues) != 1:
        raise AssertionError('Expected exact, unique queue and error classes')
    queue = copy.deepcopy(queues[0])
    queue.body = [n for n in queue.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    if len(queue.body) != len(METHODS) or {n.name for n in queue.body} != METHODS:
        raise AssertionError('Missing or repeated selected methods')
    maxima = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'MAX_ATTEMPTS' for t in n.targets)]
    if len(maxima) != 1 or not isinstance(maxima[0].value, ast.Constant) or maxima[0].value.value != 5:
        raise AssertionError('Unexpected retry threshold')
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                           maxima[0], errors[0], queue], type_ignores=[])
    scope = {'_now': lambda: 'fixed-now', 'uuid4': lambda: SimpleNamespace(hex='new-token')}
    exec(compile(ast.fix_missing_locations(code), str(SOURCE), 'exec'), scope)
    return scope


def job(job_id='j', status='queued', attempts=4, token='token'):
    return dict(job_id=job_id, status=status, attempts=attempts, claim_token=token, last_error='old')


class MemoryQueue:
    def __init__(self, jobs=(), dead=0):
        self.scope = extract_types()
        self.error_type = self.scope['L1QueueStateError']
        self.queue = self.scope['L1ExtractQueue'].__new__(self.scope['L1ExtractQueue'])
        self.queue.lock_path = 'fake-state'
        self.queue.consumer_lock_path = 'fake-consumer'
        self.state = {'jobs': copy.deepcopy(list(jobs)),
                      'dead': [job('old-' + str(i), 'dead', 5) for i in range(dead)]}
        self.events = []
        self.saves = 0
        self.fail_save_at = None
        self.scope['interprocess_lock'] = self.lock
        self.queue._load = self.load
        self.queue._save = self.save

    def lock(self, path):
        owner = self
        class FakeLock:
            def __enter__(self):
                owner.events.append(('enter', path))
            def __exit__(self, *args):
                owner.events.append(('exit', path))
        return FakeLock()

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, payload):
        self.saves += 1
        if self.saves == self.fail_save_at:
            raise self.error_type('queue_unreadable', retryable=True)
        self.state = copy.deepcopy(payload)


def fail_handler(_):
    raise ValueError('handler failed')


class NewlyDeadTests(unittest.TestCase):
    def test_terminal_at_zero_199_and_200(self):
        for initial in (0, 199, 200):
            with self.subTest(initial=initial):
                m = MemoryQueue([job()], initial)
                result = m.queue.drain_report(fail_handler, limit=1)
                self.assertEqual(result, dict(processed=0, failed=1, newly_dead=1,
                                             pending=0, dead=min(200, initial + 1), errors=['handler failed']))
                self.assertEqual(m.state['dead'][-1], job(status='dead', attempts=5, token='new-token') | {'claimed_at': 'fixed-now', 'last_error': 'handler failed'})
                self.assertEqual(m.events[0], ('enter', 'fake-consumer'))
                self.assertEqual(m.events[-1], ('exit', 'fake-consumer'))
                self.assertEqual(m.saves, 2)

    def test_multiple_failures_cross_retention_cap(self):
        m = MemoryQueue([job(str(i)) for i in range(3)], 199)
        result = m.queue.drain_report(fail_handler, limit=3)
        self.assertEqual(result, dict(processed=0, failed=3, newly_dead=3, pending=0, dead=200,
                                     errors=['handler failed'] * 3))
        self.assertEqual([j['job_id'] for j in m.state['dead'][-3:]], ['0', '1', '2'])
        self.assertEqual(m.saves, 6)

    def test_nonterminal_failure_not_counted(self):
        m = MemoryQueue([job(attempts=0)], 200)
        result = m.queue.drain_report(fail_handler, limit=1)
        self.assertEqual(result, dict(processed=0, failed=1, newly_dead=0, pending=1, dead=200,
                                     errors=['handler failed']))
        self.assertEqual(m.state['jobs'][0], job(attempts=1, token='new-token') | {'claimed_at': 'fixed-now', 'last_error': 'handler failed'})

    def test_fail_no_match_and_predicates(self):
        for target, status, token in [('other', 'running', 'token'), ('j', 'queued', 'token'),
                                      ('j', 'running', 'wrong'), ('j', 'running', '')]:
            with self.subTest(target=target, status=status, token=token):
                m = MemoryQueue([job(status=status, attempts=5)], 200)
                before = copy.deepcopy(m.state)
                self.assertEqual(m.queue._fail(target, 'bad', claim_token=token), 0)
                self.assertEqual(m.state, before)
                self.assertEqual(m.saves, 1)
                self.assertEqual(m.events, [('enter', 'fake-state'), ('exit', 'fake-state')])

    def test_duplicate_ids_count_actual_transitions(self):
        m = MemoryQueue([job(status='running', attempts=5), job(status='running', attempts=6),
                         job(status='running', attempts=1), job('other', 'running', 5)], 200)
        self.assertEqual(m.queue._fail('j', 'x' * 600, claim_token='token'), 2)
        self.assertEqual(len(m.state['dead']), 200)
        self.assertEqual([j['last_error'] for j in m.state['dead'][-2:]], ['x' * 500] * 2)
        self.assertEqual(m.state['jobs'], [job(attempts=1) | {'last_error': 'x' * 500}, job('other', 'running', 5)])
        self.assertEqual(m.saves, 1)

    def test_recovery_count_added_to_handler_count(self):
        m = MemoryQueue([job('interrupted', 'running', 5), job('next')], 200)
        result = m.queue.drain_report(fail_handler, limit=1)
        self.assertEqual(result, dict(processed=0, failed=1, newly_dead=2, pending=0, dead=200,
                                     errors=['handler failed']))
        recovered, failed = m.state['dead'][-2:]
        self.assertEqual(recovered, {'job_id': 'interrupted', 'status': 'dead', 'attempts': 5, 'last_error': 'worker_interrupted'})
        self.assertEqual(failed['job_id'], 'next')
        self.assertEqual(m.saves, 3)

    def test_failure_save_error_keeps_count_and_handler_context(self):
        m = MemoryQueue([job('interrupted', 'running', 5), job('next')], 200)
        m.fail_save_at = 3
        with self.assertRaises(m.error_type) as caught:
            m.queue.drain_report(fail_handler, limit=1)
        exc = caught.exception
        self.assertEqual(exc.context, dict(phase='record_handler_failure', processed=0, failed=1,
                                          newly_dead=1, errors=['handler failed'], last_claimed_job_id='next',
                                          handler_completed=False, completion_recorded=False, handler_error='handler failed'))
        self.assertIsInstance(exc.__cause__, ValueError)
        self.assertEqual(str(exc.__cause__), 'handler failed')
        self.assertEqual(exc.code, 'queue_unreadable')
        self.assertTrue(exc.retryable)
        self.assertEqual(m.state['jobs'][0]['status'], 'running')
        self.assertEqual(m.state['dead'][-1]['job_id'], 'interrupted')
        self.assertEqual(m.saves, 3)
        self.assertEqual(m.events[-1], ('exit', 'fake-consumer'))

    def test_direct_fail_save_error_does_not_return_count(self):
        m = MemoryQueue([job(status='running', attempts=5)], 200)
        before = copy.deepcopy(m.state)
        m.fail_save_at = 1
        with self.assertRaises(m.error_type):
            m.queue._fail('j', 'bad', claim_token='token')
        self.assertEqual(m.state, before)
        self.assertEqual(m.events[-1], ('exit', 'fake-state'))

    def test_success_ack_and_drain_projection(self):
        m = MemoryQueue([job()], 200)
        seen = []
        self.assertEqual(m.queue.drain_report(lambda j: seen.append(j['job_id']), limit=1),
                         dict(processed=1, failed=0, newly_dead=0, pending=0, dead=200, errors=[]))
        self.assertEqual(seen, ['j'])
        self.assertEqual(m.state['jobs'], [])
        self.assertEqual(m.saves, 2)
        other = MemoryQueue([job()])
        self.assertEqual(other.queue.drain(lambda _: None, limit=1), 1)

    def test_success_without_ack_not_processed(self):
        m = MemoryQueue([job()], 200)
        m.queue._complete = lambda *args, **kwargs: False
        result = m.queue.drain_report(lambda _: None, limit=1)
        self.assertEqual(result, dict(processed=0, failed=0, newly_dead=0, pending=1, dead=200, errors=[]))
        self.assertEqual(m.saves, 1)

    def test_limit_zero_still_recovers_without_handler(self):
        m = MemoryQueue([job('interrupted', 'running', 5), job('queued')], 200)
        def never(_):
            self.fail('limit zero called handler')
        self.assertEqual(m.queue.drain_report(never, limit=0),
                         dict(processed=0, failed=0, newly_dead=1, pending=1, dead=200, errors=[]))
        self.assertEqual(m.state['jobs'], [job('queued')])
        self.assertEqual(m.saves, 1)

    def test_handler_error_truncation_and_recent_five(self):
        m = MemoryQueue([job(str(i)) for i in range(6)], 200)
        def bad(j):
            raise ValueError(j['job_id'] + 'x' * 600)
        result = m.queue.drain_report(bad, limit=6)
        self.assertEqual(result, dict(processed=0, failed=6, newly_dead=6, pending=0, dead=200,
                                     errors=[str(i) + 'x' * 499 for i in range(1, 6)]))
        self.assertEqual([j['last_error'] for j in m.state['dead'][-6:]],
                         [str(i) + 'x' * 499 for i in range(6)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
