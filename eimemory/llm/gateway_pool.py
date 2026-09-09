"""Two bounded, idle-expiring SDK clients, never resident inference models.

Each process handles one JSON-line request at a time. Request IDs and discarded
timed-out workers prevent late output from being consumed by another caller.
"""
import atexit
import json
import queue
import re
import subprocess
import threading
import time
from uuid import uuid4

from .command_client import LLMResult

_MAX_BYTES = 131072
_LOCK = threading.Lock()
_POOL = None
_KEY = None


class GatewayCompletionError(RuntimeError):
    def __init__(self, reason, diagnostics=None):
        self.reason = reason if reason in {'timeout','thinking','unauthorized','pairing','scope',
            'incomplete','invalid','model','permission','forbidden','gateway_error'} else 'gateway_error'
        super().__init__('gateway_completion_unavailable')
        self.diagnostics = {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        if diagnostics.get('gateway_stage') in {'gateway_connect', 'gateway_response'}:
            self.diagnostics['gateway_stage'] = diagnostics['gateway_stage']
        elapsed = diagnostics.get('gateway_elapsed_ms')
        if type(elapsed) in (int, float) and 0 <= elapsed <= 10000:
            self.diagnostics['gateway_elapsed_ms'] = elapsed
        session = diagnostics.get('verification_session_id')
        if isinstance(session, str) and re.fullmatch('eimemory-verification-[a-f0-9-]{36}', session):
            self.diagnostics['verification_session_id'] = session


class _Worker:
    def __init__(self, argv):
        self.process = subprocess.Popen([*argv, '--serve'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.responses = queue.Queue(maxsize=1)
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_errors, daemon=True).start()

    def _read(self):
        try:
            while not self.closed:
                line = self.process.stdout.readline(_MAX_BYTES + 1)
                if not line or len(line) > _MAX_BYTES:
                    self.responses.put_nowait(None)
                    return
                self.responses.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            self.process.stdout.close()

    def _drain_errors(self):
        try:
            total = 0
            while not self.closed:
                chunk = self.process.stderr.read(4096)
                if not chunk:
                    return
                total += len(chunk)
                if total > _MAX_BYTES:
                    self.process.kill()
                    return
        except (OSError, ValueError):
            pass
        finally:
            self.process.stderr.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=1)
        # Readers own stdout/stderr; closing stdin also prevents any later input.
        if self.process.stdin:
            self.process.stdin.close()

    def call(self, payload, timeout):
        request_id = uuid4().hex
        raw = json.dumps({'request_id':request_id, **payload}, ensure_ascii=False).encode() + b'\n'
        if len(raw) > _MAX_BYTES:
            raise ValueError('gateway_request_oversized')
        def write():
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass
        threading.Thread(target=write, daemon=True).start()
        try:
            line = self.responses.get(timeout=max(.01, timeout))
            response = json.loads(line) if line else None
            if not isinstance(response, dict) or response.get('request_id') != request_id:
                raise ValueError('gateway_response_identity_invalid')
            if response.get('error'):
                raise GatewayCompletionError(response.get('reason'), response)
            result = response.get('result')
            if not isinstance(result, dict) or not all(isinstance(result.get(k),str) and result[k]
                    for k in ('text','provider_id','model_id')):
                raise ValueError('gateway_completion_unavailable')
            return LLMResult(**{k:result[k] for k in ('text','provider_id','model_id')})
        except Exception:
            self.close()  # late answers can never enter another request
            raise


class _Pool:
    def __init__(self, argv):
        self.argv = tuple(argv)
        self.workers = []
        self.available = queue.Queue(maxsize=2)
        self.lock = threading.Lock()
        self.closed = False

    def warm(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('gateway_pool_closed')
            while len(self.workers) < 2:
                worker = _Worker(self.argv)
                self.workers.append(worker)
                self.available.put_nowait(worker)
            idle = []
            while not self.available.empty():
                worker = self.available.get_nowait()
                if worker.closed or worker.process.poll() is not None:
                    worker.close()
                    replacement = _Worker(self.argv)
                    self.workers[self.workers.index(worker)] = replacement
                    worker = replacement
                idle.append(worker)
            for worker in idle:
                self.available.put_nowait(worker)

    def complete(self, payload, timeout):
        started = time.monotonic()
        self.warm()
        worker = self.available.get(timeout=min(.05,max(.01,timeout)))
        try:
            with self.lock:
                if worker.closed or worker.process.poll() is not None:
                    worker.close()
                    replacement = _Worker(self.argv)
                    self.workers[self.workers.index(worker)] = replacement
                    worker = replacement
            return worker.call(payload, timeout-(time.monotonic()-started))
        finally:
            if not self.closed:
                self.available.put_nowait(worker)

    def close(self):
        with self.lock:
            self.closed = True
            for worker in self.workers:
                worker.close()


def close_pool():
    global _POOL, _KEY
    with _LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL, _KEY = None, None


atexit.register(close_pool)


class GatewayPoolClient:
    def __init__(self, argv, *, identity_key, timeout_seconds=9):
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
        self.identity_key = identity_key

    def _pool(self):
        global _POOL, _KEY
        key = (self.argv, self.identity_key)
        with _LOCK:
            if _KEY != key:
                if _POOL is not None:
                    _POOL.close()
                _POOL, _KEY = _Pool(self.argv), key
            return _POOL

    def prepare(self):
        self._pool().warm()

    def close(self):
        pass  # Shared processes exit after 120 idle seconds or interpreter exit.

    def complete(self, *, system_prompt, user_prompt, json_mode=False):
        return self._pool().complete({'system_prompt':system_prompt,'user_prompt':user_prompt,
            'json_mode':json_mode,'deadline_unix_ms':int((time.time()+self.timeout_seconds)*1000)},
            self.timeout_seconds)
