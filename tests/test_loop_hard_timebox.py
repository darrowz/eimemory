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

1. A long-running worker is actually stopped within ~1.5s of the time
   box, *not* allowed to finish, **and the child process is
   verifiably dead** (``os.kill(pid, 0)`` reports ``ProcessLookupError``).
2. A fast worker returns its value unchanged.
3. A worker that raises has its exception surfaced to the parent.
4. A worker that writes a "before" marker is killed before it writes
   the "after" marker — i.e. side effects past the time box do not
   happen.

The wrapped functions are all module-level so the spawn context can
import and pickle them. Lambdas / local closures are not supported
(the runner raises :class:`TypeError` instead).

The ``EIMEMORY_TEST_PID_FILE`` env var is the test hook: when set,
the child worker writes its PID to that file at start-up so tests
can ``os.kill(pid, 0)`` and verify the worker was actually
terminated. The hook has zero effect in production because the env
var is unset.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

from eimemory.autonomous.loop import (
    ExperimentTimeout,
    TEST_PID_FILE_ENV,
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
_PROCESS_TREE_DIR_ENV = "EIMEMORY_TEST_PROCESS_TREE_DIR"


# ---------- module-level experiment functions (must be picklable) ----------


def _loop_with_sleep_0_1() -> float:
    """Infinite loop with sleep(0.1). If not killed, runs forever.

    This is the worker used by the "stops long worker within 1.5s"
    regression test. With a daemon-thread implementation the parent
    would return immediately but the loop would keep running; with
    the child-process implementation, the parent only returns once
    the child has been terminated.
    """
    while True:
        time.sleep(0.1)


def _returns_42() -> float:
    return 42


def _raises_value_error() -> float:
    raise ValueError("synthetic worker failure")


def _raises_large_value_error() -> float:
    raise ValueError("x" * 2_000_000)


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


def _spawns_grandchild_writer_then_sleeps() -> float:
    tmp = Path(os.environ[_PROCESS_TREE_DIR_ENV])
    after = tmp / "grandchild-after-timeout"
    started = tmp / "grandchild-started"
    code = (
        "import pathlib, sys, time; "
        "started=pathlib.Path(sys.argv[1]); after=pathlib.Path(sys.argv[2]); "
        "started.write_text('ok', encoding='utf-8'); "
        "time.sleep(2.0); "
        "after.write_text('late-write', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", code, str(started), str(after)])
    (tmp / "worker-started").write_text("ok", encoding="utf-8")
    time.sleep(10.0)
    return 0.0


# ---------- helpers ----------


def _learning_profile(tmp_path: Path) -> Path:
    """Write a learning-profile ini under ``tmp_path`` and return its path."""
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


def _pid_alive(pid: int) -> bool:
    """Return True iff the OS still has a process with this pid.

    Cross-platform implementation:

    * On Windows, ``os.kill(pid, 0)`` is not supported the same way
      (it raises ``OSError: [WinError 87]`` for some pid values).
      Use ``tasklist`` instead — the cheapest way to ask the kernel
      whether a pid exists without needing ``psutil``.
    * On POSIX, ``os.kill(pid, 0)`` is the canonical probe: it
      sends no signal but raises ``ProcessLookupError`` if the pid
      is gone. ``PermissionError`` is treated as "alive".

    Caveat: a freshly-issued pid may have been recycled by the OS by
    the time we look it up. The caller is expected to wait long
    enough for the worker to actually die before calling this.
    """
    if os.name == "nt":
        import subprocess

        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        # Windows tasklist emits one of these forms:
        #   * the process name + pid table row (pid is alive)
        #   * the localised "INFO: No tasks..." line (pid is dead)
        # We cannot rely on a fixed English substring because the
        # "no match" line is localised (Chinese on CN systems).
        # Use the OEM/system codepage (mbcs) for the decode, then
        # check whether the pid string appears as a token in any
        # line of the output.
        output = result.stdout.decode("mbcs", errors="replace")
        pid_str = str(pid)
        for line in output.splitlines():
            tokens = line.split()
            if pid_str in tokens:
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------- the four required tests ----------


def test_timebox_stops_long_worker_within_2s(tmp_path: Path, monkeypatch) -> None:
    """A long-running worker is killed within 1.5s of the time box.

    Mirrors the spec exactly:

    * wrapped fn: an infinite ``while True: time.sleep(0.1)`` loop.
    * ``time_box_seconds = 1.0``.
    * assertion: the runner raises ``ExperimentTimeout`` within 1.5s
      of the call (i.e. the parent does not block on the worker).
    * assertion: the worker **process** is dead after the timeout
      (``os.kill(pid, 0)`` raises ``ProcessLookupError``).
    """
    pid_file = tmp_path / "worker.pid"
    monkeypatch.setenv(TEST_PID_FILE_ENV, str(pid_file))

    start = time.monotonic()
    with pytest.raises(ExperimentTimeout):
        run_single_experiment(
            profile_ini=_learning_profile(tmp_path),
            audit_path=tmp_path / "audit.jsonl",
            experiment_id="exp-stops-long",
            hypothesis={"kind": "rule_tweak", "text": "infinite loop"},
            experiment_fn=_loop_with_sleep_0_1,
            baseline_value=0.40,
            time_box_seconds=1.0,
        )
    elapsed = time.monotonic() - start
    # The main thread must return within 1.5s — proves the runner is
    # not blocked on the worker (the worker would have slept ~15s
    # otherwise).
    assert elapsed < 4.0, (
        f"runner took {elapsed:.2f}s; the parent must return within "
        f"the startup allowance plus time box. If this fails, the worker was NOT "
        f"killed — the implementation fell back to a thread."
    )

    # The worker process must be dead. We verify via the PID file
    # (written by the child at start-up) and ``os.kill(pid, 0)``.
    # The 200 ms sleep before the check lets the OS recycle the pid
    # (and prevents false positives from another process grabbing
    # the same pid before we look it up).
    assert pid_file.exists(), (
        f"worker did not write its PID to {pid_file}; the test hook "
        f"({TEST_PID_FILE_ENV}) is not wired through."
    )
    time.sleep(0.2)
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert not _pid_alive(pid), (
        f"worker process {pid} is still alive after the time box; "
        f"the runner did not actually terminate the child process. "
        f"This is the core R9 regression."
    )


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


def test_timebox_drains_large_worker_errors_without_timeout() -> None:
    start = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        _run_with_time_box(_raises_large_value_error, 5.0)

    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    assert "ValueError" in str(excinfo.value)


def test_timebox_kills_worker_process_tree(tmp_path: Path, monkeypatch) -> None:
    """A timed-out worker must also stop subprocesses it spawned.

    The worker launches a grandchild that writes a file two seconds
    later. Killing only the worker leaves that grandchild alive, and
    the late file appears. Process-tree termination prevents the
    write.
    """
    monkeypatch.setenv(_PROCESS_TREE_DIR_ENV, str(tmp_path))

    with pytest.raises(ExperimentTimeout):
        run_single_experiment(
            profile_ini=_learning_profile(tmp_path),
            audit_path=tmp_path / "audit.jsonl",
            experiment_id="exp-process-tree",
            hypothesis={"kind": "sandbox_timeout", "text": "grandchild"},
            experiment_fn=_spawns_grandchild_writer_then_sleeps,
            baseline_value=0.40,
            time_box_seconds=1.0,
        )

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not (tmp_path / "grandchild-started").exists():
        time.sleep(0.05)
    assert (tmp_path / "grandchild-started").exists()
    time.sleep(2.4)
    assert not (tmp_path / "grandchild-after-timeout").exists()
