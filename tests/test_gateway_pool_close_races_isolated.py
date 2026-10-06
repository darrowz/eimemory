"""Pure in-memory pool lifecycle regression; never import the project.

Only the _Pool class is AST-extracted. Its worker dependency is a fake and its
queue, locks, and clocks are standard-library objects. Optional --source parsing
is confined to the main guard so importing this test does not consume argv.
"""
import ast
from pathlib import Path
import queue
import threading
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/llm/gateway_pool.py'


class FakeProcess:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 0 if self.dead else None


class BeforeEnterLock:
    """Insert one scheduled action immediately before a chosen lock acquisition."""
    def __init__(self, ordinal, action):
        self.lock = threading.RLock()
        self.ordinal = ordinal
        self.action = action
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        if self.entries == self.ordinal:
            action, self.action = self.action, None
            action()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class WaitingQueue(queue.Queue):
    def __init__(self, action, worker):
        super().__init__(maxsize=2)
        self.action = action
        self.worker = worker

    def get(self, block=True, timeout=None):
        if block:
            # A busy caller returns its worker just before another caller closes
            # the pool; this waiter then receives that now-closed queued worker.
            self.put_nowait(self.worker)
            self.action()
            return super().get(block=False)
        return super().get(block=False)


class PoolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.calls = []
        self.call_hook = None
        owner = self

        class FakeWorker:
            def __init__(self, argv):
                self.argv = tuple(argv)
                self.closed = False
                self.process = FakeProcess()
                owner.created.append(self)

            def close(self):
                self.closed = True
                self.process.dead = True

            def call(self, payload, timeout):
                owner.calls.append((self, payload, timeout))
                if owner.call_hook is not None:
                    owner.call_hook(self)
                return {'ok': True}

        self.Worker = FakeWorker
        tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
        classes = [node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == '_Pool']
        self.assertEqual(len(classes), 1)
        isolated = ast.Module(body=classes, type_ignores=[])
        namespace = {'queue': queue, 'threading': threading, 'time': time,
                     '_Worker': FakeWorker}
        exec(compile(isolated, str(SOURCE), 'exec'), namespace)
        self.pool = namespace['_Pool'](('inert-worker-label',))

    def assert_closed_without_new_work(self):
        created_before, calls_before = len(self.created), len(self.calls)
        error = None
        try:
            self.pool.complete({'text': 'inert'}, .5)
        except RuntimeError as exc:
            error = str(exc)
        observed = (error, len(self.created) - created_before,
                    len(self.calls) - calls_before)
        self.assertEqual(observed, ('gateway_pool_closed', 0, 0),
                         'observed (error, newly created workers, calls)')

    def test_close_after_warm_before_checkout(self):
        self.pool.warm()
        warm = self.pool.warm

        def close_after_warm():
            warm()
            self.pool.close()

        self.pool.warm = close_after_warm
        self.assert_closed_without_new_work()

    def test_close_while_waiting_for_worker(self):
        first, second = self.Worker(()), self.Worker(())
        self.pool.workers = [first, second]
        self.pool.available = WaitingQueue(self.pool.close, first)
        self.assert_closed_without_new_work()

    def test_close_before_replacement_check(self):
        self.pool.warm()
        # complete enters warm, checkout, then replacement-check critical sections.
        self.pool.lock = BeforeEnterLock(3, self.pool.close)
        self.assert_closed_without_new_work()

    def test_already_closed_pool_rejects_warm(self):
        self.pool.close()
        self.assert_closed_without_new_work()

    def test_normal_calls_reuse_one_worker(self):
        for _ in range(3):
            self.assertEqual(self.pool.complete({'text': 'inert'}, 1), {'ok': True})
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.pool.available.qsize(), 1)
        self.assertEqual(len(self.pool.workers), 1)

    def test_dead_idle_worker_is_replaced(self):
        self.pool.warm()
        old = self.created[0]
        old.process.dead = True
        self.assertEqual(self.pool.complete({}, 1), {'ok': True})
        self.assertTrue(old.closed)
        self.assertEqual(len(self.created), 2)
        self.assertEqual(self.pool.workers, [self.created[-1]])
        self.assertEqual(self.pool.available.qsize(), 1)

    def test_death_after_checkout_is_replaced_on_open_pool(self):
        self.pool.warm()
        old = self.created[0]
        self.pool.lock = BeforeEnterLock(3, lambda: setattr(old.process, 'dead', True))
        self.assertEqual(self.pool.complete({}, 1), {'ok': True})
        self.assertTrue(old.closed)
        self.assertEqual(len(self.created), 2)
        self.assertEqual(self.pool.workers, [self.created[-1]])
        self.assertEqual(self.pool.available.qsize(), 1)

    def test_two_workers_under_concurrent_load(self):
        release = threading.Event()
        both_active = threading.Event()
        state_lock = threading.Lock()
        active = 0
        peak = 0
        failures = []

        def block_call(worker):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_active.set()
            try:
                if not release.wait(3):
                    raise AssertionError('fake call release timed out')
            finally:
                with state_lock:
                    active -= 1

        def invoke():
            try:
                self.pool.complete({}, 1)
            except Exception as exc:
                failures.append(repr(exc))

        self.call_hook = block_call
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(both_active.wait(3), 'two fake workers did not start')
            with self.assertRaises(queue.Empty):
                self.pool.complete({}, .01)
            self.assertEqual(len(self.created), 2)
            self.assertEqual(peak, 2)
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(3)
            self.pool.close()
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=SOURCE)
    SOURCE = parser.parse_args().source
    unittest.main(argv=[__file__], verbosity=2)
