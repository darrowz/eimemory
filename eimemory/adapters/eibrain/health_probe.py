"""Bound waiting for read-only health snapshots, with one worker per fragment.

A timed-out read keeps its worker until it actually finishes. Repeated probes
never start more readers for that fragment or reuse an expired healthy value.
"""
from __future__ import annotations

import threading
from time import monotonic


_FRAGMENT_TTL_SECONDS = 5.0
_FRAGMENT_WAIT_SECONDS = 0.25
_STATE_LOCK = threading.Lock()


def health_fragment_cache_ttl_seconds() -> float:
    """Expose the native cache lifetime to callers scheduling fresh observations."""
    return _FRAGMENT_TTL_SECONDS


class _FragmentProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.running = False
        self.completed_at = None
        self.value = None
        self.status = "unavailable"

    def snapshot(self, builder, *, max_age_seconds=None):
        lifetime = _FRAGMENT_TTL_SECONDS if max_age_seconds is None else max_age_seconds
        with self.lock:
            if self.completed_at is not None and monotonic() - self.completed_at < lifetime:
                return self.value, self.status
            launch = not self.running
            if launch:
                self.running = True
                self.done = threading.Event()
            done = self.done
        if launch:
            try:
                threading.Thread(
                    target=self._refresh, args=(builder,), daemon=True,
                    name="eimemory-health-snapshot",
                ).start()
            except RuntimeError:
                self._complete(None, "unavailable")
        if not done.wait(_FRAGMENT_WAIT_SECONDS):
            return None, "timeout"
        with self.lock:
            # Another refresh may have started after this completion expired.
            if self.running or self.done is not done:
                return None, "timeout"
            return self.value, self.status

    def _refresh(self, builder):
        try:
            value = builder()
        except Exception:
            self._complete(None, "unavailable")
        else:
            self._complete(value, "available")

    def _complete(self, value, status):
        with self.lock:
            self.value = value
            self.status = status
            self.completed_at = monotonic()
            self.running = False
            self.done.set()


def bounded_health_fragment(runtime, name, builder):
    # Runtime-local state prevents two connections to the same database from
    # sharing migration results. Only the two fixed health fragments use this.
    with _STATE_LOCK:
        probes = getattr(runtime, "_rpc_health_probes", None)
        if probes is None:
            probes = {}
            runtime._rpc_health_probes = probes
        probe = probes.get(name)
        if probe is None:
            probe = _FragmentProbe()
            probes[name] = probe
    # Candidate readiness has its own generation-aware, authority-fenced TTL.
    # A second five-second cache would hide a newly known real query failure.
    # Keep this fragment's worker/single flight/wait bound, but never reuse its
    # completed candidate payload. Storage migration snapshots retain the TTL.
    return probe.snapshot(builder, max_age_seconds=0 if name == "candidate_health" else None)
