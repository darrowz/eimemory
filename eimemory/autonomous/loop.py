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
       are written to the audit log as ``outcome=timeout``. The
       worker is run in a **child process** (spawn context) so a
       timeout terminates the worker; the worker is **not** left
       running and can no longer mutate state.
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
API, or push to a remote. It is the per-step orchestrator used by
manual or experimental loops; production scheduling belongs to the
governance learning path.

.. note::
   The wrapped ``experiment_fn`` **must be picklable and
   self-contained** because the runner executes it in a child
   process (``multiprocessing.get_context("spawn")``). That means:

   * define the function at module level, not as a ``lambda`` or
     nested closure;
   * do not capture shared state (file handles, locks, sockets) from
     the parent process — the child cannot see them;
   * do not rely on in-process singletons — they are not shared.

   If a non-picklable function is supplied, the runner raises
   :class:`TypeError` (fail-closed) rather than falling back to a
   thread that the parent cannot actually stop.

   Windows note: ``multiprocessing.spawn`` is used so the
   implementation works the same on Windows, macOS, and Linux. The
   cost is a one-time per-process cold-start of ~200-500 ms on
   Windows; sub-second time boxes need to account for this.

   Test hook: setting the ``EIMEMORY_TEST_PID_FILE`` environment
   variable to a writable path causes the child worker to write its
   own PID to that file at start-up. Tests use this to verify
   ``os.kill(pid, 0)`` reports the worker as dead after the timeout.
"""
from __future__ import annotations

import multiprocessing
import os
import pickle
import queue as queue_module
import signal
import subprocess
import time
import warnings
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

# Substring hints used to flag a wrapped function that *looks* like it
# has external side effects. The runner only emits a warning (it does
# not refuse the call) — the function is still terminated on timeout
# because the child process is killed.
_SIDE_EFFECT_NAME_HINTS = ("write", "save", "persist", "patch", "file", "upload")

# How long to wait between SIGTERM and SIGKILL when shutting down a
# runaway child process. Two seconds is enough for a well-behaved
# process to honour the signal-handler hook; if the process is
# stuck in a C extension or busy-wait loop, the second ``kill()`` is
# the only thing that will free the CPU.
_TERMINATE_GRACE_SECONDS = 2.0

# Test-only env var. When set, the child worker writes its PID to
# the file at this path so tests can verify the worker was actually
# terminated (``os.kill(pid, 0)`` must raise ``ProcessLookupError``).
# This has zero effect in production because the env var is unset.
_TEST_PID_FILE_ENV = "EIMEMORY_TEST_PID_FILE"


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
            returns the candidate metric value. **Must be a
            picklable, module-level function** (see module docstring).
            Lambdas, local closures, and ``functools.partial``
            wrappers are not supported; passing one raises
            :class:`TypeError`.
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
            ``time_box_seconds``. The child process has already been
            terminated at this point.
        TypeError: ``experiment_fn`` is not picklable. The runner
            fails closed rather than falling back to a thread.
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
    try:
        candidate = _run_with_time_box(experiment_fn, time_box_seconds)
    except _TimeBoxTimeout as timeout_exc:
        elapsed = time.monotonic() - t0
        finished = _now_iso()
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
        raise ExperimentTimeout(elapsed=elapsed, time_box_seconds=time_box_seconds) from timeout_exc
    elapsed = time.monotonic() - t0
    finished = _now_iso()

    # ---- 4. keep / discard ----
    if baseline_value <= 0:
        if candidate > baseline_value:
            improvement = float("inf")
        elif candidate == baseline_value:
            improvement = 0.0
        else:
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


class _TimeBoxTimeout(Exception):
    """Internal-only exception raised by ``_run_with_time_box`` on timeout.

    The public runner catches this and re-raises it as
    :class:`ExperimentTimeout` (which carries the elapsed time and
    cap). Keeping an internal type lets the runner distinguish a
    timeout from a worker-side failure.
    """


def _time_box_worker(fn: Callable[[], Any], queue: Any) -> None:
    """Top-level worker for the child process.

    Must remain importable from the module so the spawn context can
    import it without circular references. Catches **every**
    exception (``BaseException``) so that even ``KeyboardInterrupt`` /
    ``SystemExit`` in the child are reported as an error rather than
    leaving the parent waiting forever.
    """
    _isolate_worker_process_group()
    # Test-only hook: if ``EIMEMORY_TEST_PID_FILE`` is set, write
    # this child's PID to that file so tests can verify the worker
    # was actually terminated. Errors writing the file are silently
    # ignored — production code never sets the env var.
    pid_file = os.environ.get(_TEST_PID_FILE_ENV)
    if pid_file:
        try:
            Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
    try:
        result = fn()
    except BaseException as exc:  # noqa: BLE001 — converted to RuntimeError in parent
        try:
            queue.put(("error", type(exc).__name__, str(exc)))
        except Exception:
            # If we cannot even enqueue the error, the parent will
            # observe a dead process and surface that as a runtime
            # error. Nothing more we can do.
            pass
        return
    try:
        queue.put(("ok", result))
    except Exception:
        pass


def _isolate_worker_process_group() -> None:
    """Put the worker in its own process group where the OS supports it."""
    if os.name == "nt" or not hasattr(os, "setpgrp"):
        return
    try:
        os.setpgrp()
    except OSError:
        pass


def _is_picklable(value: Any) -> bool:
    """Return True iff ``value`` can be pickled by the default protocol.

    Used to fail closed before spawning a child process when the
    caller's function is a lambda or local closure.
    """
    try:
        pickle.dumps(value)
    except Exception:
        return False
    return True


def _looks_like_side_effect(fn: Any) -> bool:
    """Heuristic: does the function name hint at external side effects?

    Used only to emit a ``UserWarning`` when the worker is about to
    be killed. The runner does not refuse the call — the child
    process is terminated either way — but operators deserve a
    visible signal that a function with a name like ``write_xxx``
    may have left a half-written file behind.
    """
    name = getattr(fn, "__name__", "") or ""
    name = name.lower()
    return any(hint in name for hint in _SIDE_EFFECT_NAME_HINTS)


def _terminate_process_tree(process: multiprocessing.Process, *, force: bool) -> None:
    """Terminate the worker and best-effort terminate descendants too."""
    pid = process.pid
    if not pid:
        return
    if os.name == "nt":
        # On Windows, non-forced taskkill can let console subprocesses
        # outlive their parent. Use /F on the first pass so /T still
        # has the original parent/child tree to work with.
        args = ["taskkill", "/F", "/T", "/PID", str(pid)]
        try:
            subprocess.run(args, check=False, capture_output=True, timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif hasattr(os, "killpg"):
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if force:
        process.kill()
    else:
        process.terminate()


def _run_with_time_box(
    fn: Callable[[], Any],
    time_box_seconds: float,
) -> Any:
    """Run ``fn`` in a child process, return its result, or raise on timeout.

    The child runs in the ``spawn`` start method so the worker can be
    **terminated** when the time box expires. With a daemon thread
    (the previous implementation) the worker kept running and could
    still mutate state after the runner had already declared the
    experiment ``timeout`` — that is the bug this code fixes.

    Contract:

    * Returns ``fn()`` if the worker finishes within the time box.
    * Raises :class:`_TimeBoxTimeout` if the time box expires. The
      child process is terminated (and, if necessary, killed) before
      this returns. The public runner converts the internal timeout
      into :class:`ExperimentTimeout` for the audit log.
    * Raises :class:`RuntimeError` if the worker raised. The error
      text carries the original exception type name and message.
    * Raises :class:`TypeError` if ``fn`` is not picklable (e.g. a
      ``lambda`` or local closure). The runner fails closed rather
      than silently falling back to a thread.
    """
    # The picklability check is best-effort: it catches lambdas and
    # closures up front. If the function passes the check but the
    # underlying spawn context still fails (rare; e.g. transitively
    # captures an unpicklable object), the start() call below
    # converts that failure into TypeError too.
    if not _is_picklable(fn):
        raise TypeError(
            "time-boxed experiment_fn must be picklable and self-contained "
            "(module-level function, no shared state with the parent process). "
            "Lambdas, local closures, and functools.partial over a lambda are "
            "not supported in the child-process implementation."
        )

    ctx = multiprocessing.get_context("spawn")
    queue: Any = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_time_box_worker, args=(fn, queue), name="loop-experiment")
    try:
        process.start()
    except Exception as exc:
        raise TypeError(
            "time-boxed experiment_fn must be picklable and self-contained "
            "(module-level function, no shared state with the parent process)"
        ) from exc

    deadline = time.monotonic() + max(0.0, float(time_box_seconds))
    message: tuple[Any, ...] | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            message = tuple(queue.get(timeout=min(0.05, remaining)))
            break
        except queue_module.Empty:
            if not process.is_alive():
                try:
                    message = tuple(queue.get_nowait())
                except Exception:
                    message = None
                break

    if message is None and not process.is_alive():
        try:
            message = tuple(queue.get_nowait())
        except Exception:
            message = None

    if message is None and process.is_alive():
        # Timeout: terminate the worker so it can no longer mutate
        # state. SIGTERM first (graceful), then SIGKILL after a short
        # grace window if the process is still alive (busy-wait or
        # stuck in a C extension).
        if _looks_like_side_effect(fn):
            warnings.warn(
                f"experiment_fn {fn.__name__!r} was terminated for exceeding the "
                f"time box of {time_box_seconds:.2f}s; check for half-written "
                "files, partial uploads, or other unobservable state.",
                UserWarning,
                stacklevel=2,
            )
        _terminate_process_tree(process, force=False)
        process.join(_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            _terminate_process_tree(process, force=True)
            process.join()
        process.close()
        raise _TimeBoxTimeout(time_box_seconds)

    if message is None:
        process.close()
        raise RuntimeError(
            "time-boxed function exited without reporting a result"
        )

    process.join(_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        _terminate_process_tree(process, force=True)
        process.join()
    process.close()
    status, *payload = message

    if status == "ok":
        return payload[0]
    # status == "error" — payload is (type_name, message)
    if len(payload) >= 2:
        raise RuntimeError(f"time-boxed function failed: {payload[0]}: {payload[1]}")
    raise RuntimeError(f"time-boxed function failed: {payload!r}")


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


# Expose the test hook name for tests that want to use it without
# importing the private module attribute.
TEST_PID_FILE_ENV = _TEST_PID_FILE_ENV
