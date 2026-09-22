"""Request-owned circuit tickets; no transport, persistence, or Runtime imports."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Callable


@dataclass(frozen=True, slots=True)
class CircuitTicket:
    generation: int
    probe: bool = False


class CircuitBreaker:
    def __init__(
        self, *, failure_threshold: int, reset_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if type(failure_threshold) is not int or failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if not isfinite(reset_seconds) or reset_seconds <= 0:
            raise ValueError("reset_seconds must be finite and positive")
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._lock = Lock()
        self._generation = 0
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def acquire(self) -> CircuitTicket | None:
        with self._lock:
            if self._opened_at is None:
                return CircuitTicket(self._generation)
            if self._probe_in_flight or self._clock() - self._opened_at < self.reset_seconds:
                return None
            self._probe_in_flight = True
            return CircuitTicket(self._generation, probe=True)

    def is_open(self) -> bool:
        """Report admission state without reserving the recovery probe."""
        with self._lock:
            return self._opened_at is not None and (
                self._probe_in_flight or self._clock() - self._opened_at < self.reset_seconds
            )

    def succeeded(self, ticket: CircuitTicket) -> None:
        with self._lock:
            if ticket.generation != self._generation:
                return
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False
            if ticket.probe:
                self._generation += 1

    def failed(self, ticket: CircuitTicket) -> None:
        with self._lock:
            if ticket.generation != self._generation:
                return
            self._failures += 1
            if ticket.probe or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()
                self._probe_in_flight = False
                # Completions from calls admitted before this opening cannot
                # close it, restart its clock, or consume a new probe.
                self._generation += 1

    def abandon(self, ticket: CircuitTicket) -> None:
        """Release a probe for local validation errors or caller cancellation."""
        with self._lock:
            if ticket.generation == self._generation and ticket.probe:
                self._probe_in_flight = False
