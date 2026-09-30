"""Pooled transport for the stdin/stdout Luna review bridge.

Transport only: the worker runs the bridge's unchanged ``complete`` with the
same request fields (prompts, json_mode, deadline, per-channel route) as the
one-shot ``CommandLLMClient``. The pool removes per-call interpreter start,
Hermes bootstrap/import and provider-client setup (about 2.3s measured on
honrui), which otherwise pushed proactive recall past the Hermes host's fixed
8s prefetch window.

Bounded: at most two workers, one request per worker at a time. When both are
busy or a worker cannot be started, the call falls back to the ordinary
one-shot command, so the pool never reduces concurrency. A worker that errors
or times out is discarded, so a late answer can never reach another request.
"""
from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from dataclasses import replace

from .command_client import CommandCompletionError, CommandLLMClient, LLMResult, current_verifier_route
from .completion_timing import safe_child_timing, safe_timing

LUNA_BRIDGE_SUFFIX = '/deploy/luna_bridge/luna_review_command.py'
_LOCK = threading.Lock()
_POOL = None
_KEY = None


def is_luna_bridge(argv) -> bool:
    return bool(argv) and str(argv[-1]).replace('\\', '/').endswith(LUNA_BRIDGE_SUFFIX)


def pool_enabled(argv) -> bool:
    """On by default for the Luna bridge; ``EIMEMORY_RECALL_BRIDGE_POOL=0`` disables it."""
    return is_luna_bridge(argv) and os.environ.get('EIMEMORY_RECALL_BRIDGE_POOL', '1').strip() != '0'


class _BridgeWorker:
    """gateway_pool worker protocol, with bridge error frames mapped to failures."""

    def __init__(self, argv):
        from .gateway_pool import _Worker
        self._worker = _Worker(argv)

    @property
    def closed(self):
        return self._worker.closed

    @property
    def process(self):
        return self._worker.process

    def close(self):
        self._worker.close()

    def call(self, payload, timeout):
        import json
        from uuid import uuid4
        worker = self._worker
        request_id = uuid4().hex
        raw = json.dumps({'request_id': request_id, **payload}, ensure_ascii=False).encode() + b'\n'
        if len(raw) > 131072 + 256:
            raise ValueError('bridge_request_oversized')

        def write():
            try:
                worker.process.stdin.write(raw)
                worker.process.stdin.flush()
            except (OSError, ValueError):
                pass

        threading.Thread(target=write, daemon=True).start()
        try:
            line = worker.responses.get(timeout=max(.01, timeout))
            response = json.loads(line) if line else None
            if not isinstance(response, dict) or response.get('request_id') != request_id:
                raise ValueError('bridge_response_identity_invalid')
            diagnostics = safe_child_timing(response.get('diagnostics'))
            if response.get('error'):
                error = CommandCompletionError(str(response.get('reason') or ''))
                error.completion_timing = safe_timing(diagnostics)
                raise error
            result = response.get('result')
            if not isinstance(result, dict) or not all(
                    isinstance(result.get(k), str) and result[k].strip()
                    for k in ('text', 'provider_id', 'model_id')):
                raise ValueError('bridge_completion_unavailable')
            return LLMResult(text=result['text'].strip(), provider_id=result['provider_id'].strip(),
                             model_id=result['model_id'].strip(),
                             diagnostics={**safe_child_timing(result.get('diagnostics')), 'command_pooled': True})
        except BaseException:
            self.close()  # late answers can never enter another request
            raise


class _BridgePool:
    def __init__(self, argv, size=2):
        self.argv = tuple(argv)
        self.size = size
        self.workers = []
        self.idle = []
        self.lock = threading.Lock()
        self.closed = False

    def _spawn(self):
        worker = _BridgeWorker(self.argv)
        self.workers.append(worker)
        return worker

    def _alive(self, worker):
        return not worker.closed and worker.process.poll() is None

    def warm(self):
        """Start one idle worker if none is alive; never blocks on a model call."""
        with self.lock:
            if self.closed:
                return
            self.workers = [w for w in self.workers if self._alive(w)]
            self.idle = [w for w in self.idle if self._alive(w)]
            if not self.idle and len(self.workers) < self.size:
                self.idle.append(self._spawn())

    def acquire(self):
        """An idle live worker, a new one within the bound, or None when saturated."""
        with self.lock:
            if self.closed:
                return None
            self.workers = [w for w in self.workers if self._alive(w)]
            while self.idle:
                worker = self.idle.pop()
                if self._alive(worker):
                    return worker
            if len(self.workers) < self.size:
                return self._spawn()
            return None

    def release(self, worker):
        with self.lock:
            if not self.closed and self._alive(worker):
                self.idle.append(worker)

    def close(self):
        with self.lock:
            self.closed = True
            for worker in self.workers:
                worker.close()
            self.workers, self.idle = [], []


def _shared_pool(argv, identity_key):
    global _POOL, _KEY
    key = (tuple(argv), identity_key)
    with _LOCK:
        if _KEY != key:
            if _POOL is not None:
                _POOL.close()
            _POOL, _KEY = _BridgePool(argv), key
        return _POOL


def close_pool():
    global _POOL, _KEY
    with _LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL, _KEY = None, None


atexit.register(close_pool)


class BridgePoolClient:
    """Drop-in for ``CommandLLMClient`` (argv, timeout_seconds, prepare, close, complete)."""

    def __init__(self, argv, *, identity_key, timeout_seconds=90):
        self.argv = tuple(argv)
        self.identity_key = identity_key
        self.timeout_seconds = timeout_seconds

    def _pool(self):
        return _shared_pool(self.argv, self.identity_key)

    def prepare(self):
        try:
            self._pool().warm()
        except OSError:
            pass

    def close(self):
        pass  # shared workers exit on idle/request bounds or interpreter exit

    def _one_shot(self, system_prompt, user_prompt, json_mode, reason):
        client = CommandLLMClient(self.argv, timeout_seconds=max(1, int(self.timeout_seconds)))
        result = client.complete(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=json_mode)
        return replace(result, diagnostics={**(result.diagnostics or {}), 'command_pooled': False,
                                            'pool_fallback': reason})

    def complete(self, *, system_prompt, user_prompt, json_mode=False):
        started = time.monotonic()
        timeout = float(self.timeout_seconds)
        payload = {
            'system_prompt': str(system_prompt),
            'user_prompt': str(user_prompt),
            'json_mode': bool(json_mode),
            'deadline_unix_ms': int((time.time() + timeout) * 1000),
        }
        route = current_verifier_route()
        if route:
            for key in ('provider', 'model', 'fallback_provider', 'fallback_model'):
                value = str(route.get(key) or '').strip()
                if value:
                    payload[key] = value
        pool = self._pool()
        try:
            worker = pool.acquire()
        except OSError:
            return self._one_shot(system_prompt, user_prompt, json_mode, 'spawn_failed')
        if worker is None:
            return self._one_shot(system_prompt, user_prompt, json_mode, 'pool_saturated')
        try:
            return worker.call(payload, timeout - (time.monotonic() - started))
        except queue.Empty:
            error = TimeoutError('bridge_pool_timeout')
            error.completion_timing = {}
            raise error from None
        finally:
            pool.release(worker)
