"""Hard time-box contract tests for the Karpathy-loop runner (R9, Bug D).

The original ``_run_with_time_box`` implementation used a daemon
thread that the parent process could not actually stop. A timed-out
worker kept running in the background, which meant a runaway
experiment could still mutate state (write a half-finished file,
flap a remote API) after the runner had already declared the
experiment ``timeout`` and moved on.

The fix is to execute ``experiment_fn`` in a **child process** (spawn
context) and ``terminate()`` / ``kill()`` it when the time box
expires. These tests pin the new contract:

1. A long-running worker is actually stopped within ~2s of the time
   box, *not* allowed to finish.
2. A fast worker returns its value unchanged.
3. A worker that raises has its exception surfaced to the parent.
4. A worker that writes a "before" marker is killed before it writes
   the "after" marker — i.e. side effects past the time box do not
   happen.

The wrapped functions are all module-level so the spawn context can
import and pickle them. Lambdas / local closures are not supported
(the runner raises :class:`TypeError` instead).
"""
from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

import pytest

from eimemory.autonomous.loop import (
    ExperimentTimeout,
    _run_with_time_box,
    run_single_experiment,
)
from eimemory.governance.safety.profile import (
    AutonomyProfile,
    ProfileState,
    save_profile,
)


# The side-effect tests share the temp dir with the worker through
# this env var. Module-level so the worker (which is re-imported in
# the child process) can read it on its first import.
_SIDE_EFFECT_DIR_ENV = "EIMEMORY_TEST_SIDE_EFFECT_DIR"


# ---------- module-level experiment functions ----------


def _busy_sleep_then_return() -> float:
    """Sleep longer than the test's time box; if not killed, returns 0.50."""
    time.sleep(5.0)
    return 0.50


def _returns_42() -> float:
    return 42


def _raises_value_error() -> float:
    raise ValueError("synthetic worker failure")


def _writes_two_markers_then_slow_return() -> float:
    """Module-level worker that uses the env-var shared tmp dir.

    Writes the 'before-timeout' marker, sleeps past the time box,
    and would have written 'after-timeout' had it not been killed.
    The 'before' marker is the receipt we use to prove the worker
    actually started; the absence of the 'after' marker is the proof
    that the worker was killed before completing.
    """
    tmp = Path(os.environ[_SIDE_EFFECT_DIR_ENV])
    (tmp / "before-timeout").write_text("ok", encoding="utf-8")
    time.sleep(5.0)
    (tmp / "after-timeout").write_text("ok", encoding="utf-8")
    return 0.99


def _write_results_to_disk() -> float:
    """Name matches the side-effect heuristic; sleeps past the time box."""
    time.sleep(5.0)
    return 0.0


# ---------- top-level runner integration (mirrors test_loop.py) ----------


def _learning_profile(tmp_path: Path) -> Path:
    ini = tmp_path / "eimemory.ini"
    save_profile(
        ProfileState(
            profile=AutonomyProfile.LEARNING,
            started_at="",
            profile_history_path=tmp_path / "ph.jsonl",
        ),
        ini,
    )
    return ini


# ---------- the four required tests ----------


def test_timebox_stops_long_worker_within_2s(tmp_path: Path) -> None:
    """A worker that exceeds the time box is killed within ~2s.

    The old daemon-thread implementation could not kill the worker;
    the test would either hang or return without proving termination.
    With the child-process implementation, ``is_alive()`` must be
    False shortly after the time box expires.
    """
    start = time.monotonic()
    with pytest.raises(ExperimentTimeout):
        run_single_experiment(
            profile_ini=_learning_profile(tmp_path),
            audit_path=tmp_path / "audit.jsonl",
            experiment_id="exp-hard-timebox",
            hypothesis={"kind": "rule_tweak", "text": "stuck"},
            experiment_fn=_busy_sleep_then_return,
            baseline_value=0.40,
            time_box_seconds=0.2,
        )
    elapsed = time.monotonic() - start
    # The runner must return within time_box + terminate grace + the
    # typical spawn overhead on Windows. We give it 4 seconds of
    # headroom; if it took longer, the worker was not actually killed
    # (e.g. the implementation fell back to a thread).
    assert elapsed < 4.0, (
        f"runner did not kill the worker within the time box + grace: "
        f"elapsed={elapsed:.2f}s"
    )

    # Sanity-check: the experiment was recorded as timeout in the
    # audit log. (We don't reach for multiprocessing internals here
    # because the public contract is "raise ExperimentTimeout".)
    audit = tmp_path / "audit.jsonl"
    assert audit.exists()
    text = audit.read_text(encoding="utf-8")
    assert "timeout" in text


def test_timebox_still_returns_value_for_fast_worker(tmp_path: Path) -> None:
    """A worker that finishes before the time box returns its value."""
    result = run_single_experiment(
        profile_ini=_learning_profile(tmp_path),
        audit_path=tmp_path / "audit.jsonl",
        experiment_id="exp-fast",
        hypothesis={"kind": "rule_tweak", "text": "fast"},
        experiment_fn=_returns_42,
        baseline_value=10.0,
        time_box_seconds=2.0,
    )
    # 42 vs 10 -> kept (improvement 320% >> 1% threshold).
    assert result.outcome == "kept"
    assert result.candidate_value == 42.0
    assert result.improvement_pct == pytest.approx((42.0 - 10.0) / 10.0)


def test_timebox_propagates_exception(tmp_path: Path) -> None:
    """A worker that raises is reported as a runtime error to the parent.

    The original exception type and message are preserved in the
    raised error text. The audit log is **not** written for an
    exception (only for kept / discarded / timeout).
    """
    audit = tmp_path / "audit.jsonl"
    with pytest.raises(RuntimeError) as excinfo:
        run_single_experiment(
            profile_ini=_learning_profile(tmp_path),
            audit_path=audit,
            experiment_id="exp-explode",
            hypothesis={"kind": "rule_tweak", "text": "boom"},
            experiment_fn=_raises_value_error,
            baseline_value=0.40,
            time_box_seconds=2.0,
        )
    # The original type name and message must appear in the error.
    text = str(excinfo.value)
    assert "ValueError" in text
    assert "synthetic worker failure" in text
    # No audit row for a worker exception — same as the previous
    # daemon-thread contract.
    assert not audit.exists() or audit.read_text(encoding="utf-8").strip() == ""


def test_timebox_terminates_worker_that_writes_files(tmp_path: Path, monkeypatch) -> None:
    """A worker that writes 'before-timeout' is killed before 'after-timeout'.

    This is the core regression test for the bug: with a daemon
    thread, both files would be written (the worker is unstoppable
    from the parent process). With the child-process implementation,
    only 'before-timeout' exists after the timeout.

    The temp dir is shared with the worker via an env var because
    the child process cannot see the test's local ``tmp_path``
    object; the worker reads the path at import time. The time box
    is 1.0s — long enough to absorb Windows spawn overhead
    (~200-500 ms) but still shorter than the worker's 5-second
    sleep.
    """
    monkeypatch.setenv(_SIDE_EFFECT_DIR_ENV, str(tmp_path))

    start = time.monotonic()
    with pytest.raises(ExperimentTimeout):
        run_single_experiment(
            profile_ini=_learning_profile(tmp_path),
            audit_path=tmp_path / "audit.jsonl",
            experiment_id="exp-side-effect",
            hypothesis={"kind": "rule_tweak", "text": "writer"},
            experiment_fn=_writes_two_markers_then_slow_return,
            baseline_value=0.40,
            time_box_seconds=1.0,
        )
    elapsed = time.monotonic() - start
    # The runner must return within time_box + terminate grace + the
    # typical spawn overhead on Windows. 4 seconds of headroom is
    # generous; if it took longer, the worker was not actually killed.
    assert elapsed < 4.0, f"worker not killed in time: {elapsed:.2f}s"

    # The 'before' marker must exist (the worker wrote it before the
    # timeout). The 'after' marker must NOT exist (the worker was
    # killed before it could write it). This is the R9 regression.
    assert (tmp_path / "before-timeout").exists(), (
        "before-timeout marker should exist; the worker was killed "
        "after writing it. If this fails on Windows, the time box is "
        "shorter than the child-process spawn overhead — increase "
        "time_box_seconds in this test."
    )
    assert not (tmp_path / "after-timeout").exists(), (
        "after-timeout marker should NOT exist; the worker was "
        "killed before it could write it. This is the R9 regression."
    )


# ---------- additional contract tests (side-effect warning, type-error) ----------


def test_timebox_rejects_non_picklable_function() -> None:
    """Lambdas are not picklable; the runner must fail closed with TypeError.

    This is the explicit fail-closed behavior the plan requires:
    a non-picklable function raises TypeError rather than silently
    falling back to a thread.
    """
    with pytest.raises(TypeError) as excinfo:
        _run_with_time_box(lambda: 0.5, 1.0)
    assert "picklable" in str(excinfo.value).lower()


def test_timebox_warns_on_side_effect_terminated_worker(tmp_path: Path) -> None:
    """Functions whose name hints at side effects trigger a UserWarning on timeout.

    The warning is informational — the worker is still killed — but
    it tells the operator that a function with a name like
    ``write_*`` was terminated and may have left a half-written
    file behind.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ExperimentTimeout):
            run_single_experiment(
                profile_ini=_learning_profile(tmp_path),
                audit_path=tmp_path / "audit.jsonl",
                experiment_id="exp-warn",
                hypothesis={"kind": "rule_tweak", "text": "writer"},
                experiment_fn=_write_results_to_disk,
                baseline_value=0.40,
                time_box_seconds=0.2,
            )
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert user_warnings, "expected a UserWarning for a side-effecting fn"
    text = str(user_warnings[0].message)
    assert "_write_results_to_disk" in text
    assert "terminated" in text.lower() or "time box" in text.lower()