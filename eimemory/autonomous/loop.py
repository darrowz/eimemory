"""Karpathy Loop single experiment runner with hard time box + profile gate.

This is the Phase 2 main body of the autonomous learning loop
(plan: ``docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md``
Task 2.2). Each call to :func:`run_single_experiment` is **one
experiment** in the autoresearch loop. The runner enforces the
contract that keeps the loop safe to run unattended in the sandbox:

    1. **Profile-gate** — ``load_profile().can_run_phase2()`` must be
       True. ``conservative`` (the default on a fresh install) raises
       :class:`ProfileBlocked` and writes nothing to the audit log
       (the experiment never started; the side-effect ledger stays
       clean).
    2. **Circuit-breaker** — ``consume('code_patch_write')`` must
       succeed. The default budget is 3 per hour
       (``circuit_breaker.DEFAULT_BUDGETS['code_patch_write']``); the
       4th call inside the rolling 1-hour window raises
       :class:`BudgetExceeded` and the experiment is **not** recorded
       as ``kept``/``discarded`` (only the gate failure is logged).
    3. **Time-box** — ``experiment_fn`` must finish within
       ``time_box_seconds`` (default 300 s, the plan's 5-minute cap).
       Long-running experiments raise :class:`ExperimentTimeout` and
       are written to the audit log as ``outcome=timeout``.
    4. **Keep / discard** — candidate metric vs. baseline; keep if
       relative improvement >= ``keep_threshold`` (default 1%),
       otherwise discard. The decision is deterministic and
       fail-closed: under-threshold is ``discarded``, never ``kept``.
    5. **Audit trail** — every experiment that passes the gates
       (kept / discarded / timeout) appends one row to
       ``audit.jsonl`` with ``action_class=code_patch_write`` and the
       outcome. The audit log is append-only with a sha256 chain
       (``eimemory.governance.safety.audit``); a tamper-detect
       verifier runs hourly.

The runner does **not** itself mutate the live state, call any paid
API, or push to a remote. It is the per-step orchestrator; the
promotion state machine and the nightly cron are separate modules.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from eimemory.governance.safety.audit import AuditLog
from eimemory.governance.safety.circuit_breaker import (
    BudgetExceeded,
    CircuitBreaker,
)
from eimemory.governance.safety.profile import load_profile


# Action class used in the audit log and the circuit breaker.
# Both the breaker and the audit must use the same name so a future
# operator inspecting the audit can correlate a row with the breaker
# state for the same hour window.
ACTION_CLASS_CODE_PATCH_WRITE = "code_patch_write"

# The Phase 2 plan specifies a 5-minute (300 s) hard time box per
# experiment. Configurable for tests; the test suite overrides this
# with much shorter values (sub-second) so a misbehaving experiment
# cannot slow the suite.
DEFAULT_TIME_BOX_SECONDS = 300.0

# 1% relative improvement is the Phase 2 plan's "keep" threshold.
# Below this relative delta the experiment is discarded; equal-to or
# above is kept (inclusive on the boundary to avoid fence-post
# off-by-one on the first kept experiment).
DEFAULT_KEEP_THRESHOLD = 0.01


# ---------- exception types ----------

class ProfileBlocked(Exception):
    """Raised when the current autonomy profile forbids Phase 2 work.

    Attributes:
        profile: The profile name that blocked the experiment
            (e.g. ``"conservative"``).
    """

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(f"profile_blocked: {profile} cannot run Phase 2")


class ExperimentTimeout(Exception):
    """Raised when ``experiment_fn`` does not finish in time.

    Attributes:
        elapsed: Seconds the runner actually waited before giving up.
        time_box_seconds: The configured cap.
    """

    def __init__(self, elapsed: float, time_box_seconds: float) -> None:
        self.elapsed = elapsed
        self.time_box_seconds = time_box_seconds
        super().__init__(
            f"experiment_timeout: ran for {elapsed:.2f}s, cap {time_box_seconds:.2f}s"
        )


# ---------- result dataclass ----------

@dataclass(slots=True)
class ExperimentResult:
    """The outcome of a single experiment.

    Attributes:
        experiment_id: Caller-supplied id (used in audit + log).
        outcome: One of ``"kept"``, ``"discarded"``, ``"timeout"``.
            ``ProfileBlocked`` and ``BudgetExceeded`` are gate
            failures, not outcomes, and do not produce a result.
        metric_name: The metric the experiment was scored on
            (default ``"recall_view.hit_at_1"``).
        baseline_value: Baseline value passed in.
        candidate_value: Value returned by ``experiment_fn`` (None
            for timeout, since the function did not complete).
        improvement_pct: ``(candidate - baseline) / baseline``, or
            ``None`` for timeout.
        started_at: ISO-8601 UTC timestamp of the run start.
        finished_at: ISO-8601 UTC timestamp of the run end.
        duration_seconds: ``finished_at - started_at`` in seconds.
        hypothesis: Caller-supplied hypothesis dict (echoed back).
        error: Optional human-readable error string (timeout reason,
            etc.). ``None`` for kept / discarded.
    """

    experiment_id: str
    outcome: str
    metric_name: str
    baseline_value: float
    candidate_value: float | None
    improvement_pct: float | None
    started_at: str
    finished_at: str
    duration_seconds: float
    hypothesis: dict = field(default_factory=dict)
    error: str | None = None


# ---------- the runner ----------

def run_single_experiment(
    *,
    profile_ini: Path,
    audit_path: Path,
    experiment_id: str,
    hypothesis: dict,
    experiment_fn: Callable[[], float],
    baseline_value: float,
    metric_name: str = "recall_view.hit_at_1",
    time_box_seconds: float = DEFAULT_TIME_BOX_SECONDS,
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
    circuit_breaker: CircuitBreaker | None = None,
    audit_log: AuditLog | None = None,
) -> ExperimentResult:
    """Run a single Karpathy-loop experiment end-to-end.

    Args:
        profile_ini: Path to ``eimemory.ini``. The runner reads the
            current profile from this file. ``conservative`` blocks
            the run with :class:`ProfileBlocked`.
        audit_path: Path to ``state/audit.jsonl``. Every experiment
            that passes the gates appends one row.
        experiment_id: Caller-supplied id; echoed in the result and
            the audit row. Must be unique per experiment.
        hypothesis: Free-form dict describing the change being tried.
            Echoed in the result and the audit row.
        experiment_fn: Zero-arg callable that runs the experiment and
            returns the candidate metric value. The runner enforces
            the time box via a thread + join; the function itself
            does not need to be cooperative.
        baseline_value: The current ``metric_name`` value; the
            experiment is compared against this.
        metric_name: Name of the metric being optimised. Default
            ``"recall_view.hit_at_1"`` per the Phase 2 spec.
        time_box_seconds: Hard cap on ``experiment_fn`` runtime.
            Default 300 s per the plan.
        keep_threshold: Minimum relative improvement required to
            keep. Default 0.01 (1%). Boundary is inclusive.
        circuit_breaker: Optional :class:`CircuitBreaker`. If ``None``
            the runner uses ``"code_patch_write"`` against a fresh
            in-memory breaker rooted at ``audit_path.parent``.
            Pass a shared instance to make the budget global.
        audit_log: Optional pre-built :class:`AuditLog`. If ``None``
            one is constructed from ``audit_path``.

    Returns:
        :class:`ExperimentResult` with the outcome and metric
        metadata.

    Raises:
        ProfileBlocked: Profile is not allowed to run Phase 2.
        BudgetExceeded: Circuit-breaker budget for
            ``"code_patch_write"`` is exhausted for the current hour.
        ExperimentTimeout: ``experiment_fn`` did not finish within
            ``time_box_seconds``.
    """
    # ---- 1. profile gate ----
    profile = load_profile(Path(profile_ini))
    if not profile.can_run_phase2():
        raise ProfileBlocked(profile.profile.value)

    # ---- 2. circuit breaker ----
    cb = circuit_breaker or CircuitBreaker(
        root=Path(audit_path).parent / "circuit_breaker"
    )
    cb.consume(ACTION_CLASS_CODE_PATCH_WRITE)

    # ---- 3. time-boxed experiment ----
    started = _now_iso()
    t0 = time.monotonic()
    candidate = _run_with_time_box(experiment_fn, time_box_seconds)
    elapsed = time.monotonic() - t0
    finished = _now_iso()

    if candidate is _TIMED_OUT_SENTINEL:
        result = ExperimentResult(
            experiment_id=experiment_id,
            outcome="timeout",
            metric_name=metric_name,
            baseline_value=baseline_value,
            candidate_value=None,
            improvement_pct=None,
            started_at=started,
            finished_at=finished,
            duration_seconds=elapsed,
            hypothesis=dict(hypothesis),
            error=f"ran for {elapsed:.2f}s, cap {time_box_seconds:.2f}s",
        )
        _append_audit(
            audit_log or AuditLog(Path(audit_path)),
            result,
        )
        raise ExperimentTimeout(elapsed=elapsed, time_box_seconds=time_box_seconds)

    # ---- 4. keep / discard ----
    if baseline_value <= 0:
        # Fail-closed: with a non-positive baseline, the relative
        # improvement is undefined. Discard to avoid a divide-by-zero
        # / "negative means infinite improvement" trap.
        improvement = float("-inf")
    else:
        improvement = (candidate - baseline_value) / baseline_value
    outcome = "kept" if improvement >= keep_threshold else "discarded"

    result = ExperimentResult(
        experiment_id=experiment_id,
        outcome=outcome,
        metric_name=metric_name,
        baseline_value=baseline_value,
        candidate_value=candidate,
        improvement_pct=improvement,
        started_at=started,
        finished_at=finished,
        duration_seconds=elapsed,
        hypothesis=dict(hypothesis),
        error=None,
    )
    _append_audit(audit_log or AuditLog(Path(audit_path)), result)
    return result


# ---------- internals ----------

# Sentinel returned by ``_run_with_time_box`` when the time box
# expires.  Using a module-level object lets the public runner
# distinguish a "no value because the function timed out" from a
# legitimate ``0.0`` candidate value.
_TIMED_OUT_SENTINEL: Any = object()


def _run_with_time_box(
    fn: Callable[[], float],
    time_box_seconds: float,
) -> Any:
    """Run ``fn`` in a worker thread, return its result, or sentinel on timeout.

    The thread cannot be killed from the outside in Python; the
    function continues running in the background after a timeout, but
    the runner no longer waits for it.  This matches the Phase 2
    plan's "hard time box" — the experiment is recorded as
    ``timeout`` and a new experiment can be started.  The leaked
    thread is the price of the hard cap; in the sandbox v1 it is
    acceptable because the wrapped function is a pure metric
    evaluation, not a long-lived daemon.
    """
    box: dict[str, Any] = {"value": _TIMED_OUT_SENTINEL, "error": None}
    barrier = threading.Event()

    def _worker() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 — re-raised in the main thread
            box["error"] = exc
        finally:
            barrier.set()

    thread = threading.Thread(target=_worker, name="loop-experiment", daemon=True)
    thread.start()
    barrier.wait(timeout=time_box_seconds)
    if not barrier.is_set():
        return _TIMED_OUT_SENTINEL
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


def _now_iso() -> str:
    """Return the current UTC time as ISO-8601 (with offset)."""
    return datetime.now(timezone.utc).isoformat()


def _append_audit(audit_log: AuditLog, result: ExperimentResult) -> None:
    """Write one audit row for a finished experiment.

    The row includes the runner's actor, the action class, the
    outcome, the metric metadata, and the experiment id so the row
    can be traced back to the cron tick that produced it.
    """
    audit_log.append(
        {
            "actor": "loop.py:run_single_experiment",
            "action_class": ACTION_CLASS_CODE_PATCH_WRITE,
            "experiment_id": result.experiment_id,
            "outcome": result.outcome,
            "metric_name": result.metric_name,
            "baseline_value": result.baseline_value,
            "candidate_value": result.candidate_value,
            "improvement_pct": result.improvement_pct,
            "duration_seconds": result.duration_seconds,
            "hypothesis": dict(result.hypothesis),
            "error": result.error,
        }
    )
