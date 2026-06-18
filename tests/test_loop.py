"""Tests for the Karpathy Loop single experiment runner (Phase 2, Task 2.2).

The runner is the single-step heart of the autoresearch loop:
    1. Profile-gate: ``load_profile().can_run_phase2()`` must be True
       (otherwise ``ProfileBlocked``).
    2. Circuit-breaker: ``consume('code_patch_write')`` must succeed
       (otherwise ``CircuitBreakerTrip``).
    3. Time-box: ``experiment_fn`` must finish within
       ``time_box_seconds`` (otherwise ``ExperimentTimeout``). The
       worker runs in a child process that is actually terminated on
       timeout (R9 fix).
    4. Compare candidate metric to baseline; keep if relative
       improvement >= ``keep_threshold`` (default 1%), else discard.
    5. Append an audit row with action_class=code_patch_write.

Mirrors ``eimemory/autonomous/loop.py``. RED-GREEN TDD per
``docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md`` Task 2.2.

.. note::
   ``experiment_fn`` must be picklable (the runner uses
   ``multiprocessing.get_context("spawn")``). Tests therefore define
   each function at module level below instead of using ``lambda``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from eimemory.autonomous.loop import (
    ExperimentResult,
    ExperimentTimeout,
    ProfileBlocked,
    run_single_experiment,
)
from eimemory.governance.safety.audit import AuditLog
from eimemory.governance.safety.circuit_breaker import (
    BudgetExceeded,
    CircuitBreaker,
)
from eimemory.governance.safety.profile import (
    AutonomyProfile,
    ProfileState,
    save_profile,
)


# ---------- module-level experiment functions (must be picklable) ----------


def _fn_returns_0_42() -> float:
    return 0.42


def _fn_returns_0_50() -> float:
    return 0.50


def _fn_returns_0_404() -> float:
    return 0.404


def _fn_returns_0_402() -> float:
    return 0.402


def _fn_returns_0_30() -> float:
    return 0.30


def _fn_returns_0_41() -> float:
    return 0.41


def _slow_2s() -> float:
    """Sleeps for two seconds — used to exceed a sub-second time box."""
    time.sleep(2.0)
    return 0.50


# ---------- helpers ----------


def _write_learning_profile(tmp_path: Path) -> Path:
    """Write a learning-profile ini under tmp_path and return its path."""
    ini = tmp_path / "eimemory.ini"
    save_profile(ProfileState(profile=AutonomyProfile.LEARNING, started_at="", profile_history_path=tmp_path / "ph.jsonl"), ini)
    return ini


def _write_conservative_profile(tmp_path: Path) -> Path:
    ini = tmp_path / "eimemory.ini"
    save_profile(ProfileState(profile=AutonomyProfile.CONSERVATIVE, started_at="", profile_history_path=tmp_path / "ph.jsonl"), ini)
    return ini


def _new_audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


# ---------- profile gate ----------

def test_profile_conservative_blocks_experiment(tmp_path: Path):
    """conservative profile must raise ProfileBlocked before touching anything."""
    ini = _write_conservative_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    with pytest.raises(ProfileBlocked) as excinfo:
        run_single_experiment(
            profile_ini=ini,
            audit_path=audit,
            experiment_id="exp-001",
            hypothesis={"kind": "rule_tweak", "text": "lower k from 8 to 4"},
            experiment_fn=_fn_returns_0_42,
            baseline_value=0.40,
            time_box_seconds=5.0,
        )
    assert excinfo.value.profile == "conservative"
    # No audit row should be written for a profile block: the experiment
    # never started, so the side-effect ledger stays clean.
    assert not audit.exists() or audit.read_text(encoding="utf-8").strip() == ""


def test_profile_learning_allows_experiment(tmp_path: Path):
    """learning profile must let the experiment run to completion."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-002",
        hypothesis={"kind": "rule_tweak", "text": "x"},
        experiment_fn=_fn_returns_0_50,
        baseline_value=0.40,
        time_box_seconds=5.0,
    )
    assert result.outcome in {"kept", "discarded"}


# ---------- keep / discard decision ----------

def test_kept_on_relative_improvement_at_or_above_one_percent(tmp_path: Path):
    """Candidate = baseline * 1.01 must be kept (threshold inclusive)."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-keep",
        hypothesis={"kind": "rule_tweak", "text": "+"},
        experiment_fn=_fn_returns_0_404,
        baseline_value=0.40,
        keep_threshold=0.01,
        time_box_seconds=5.0,
    )
    assert result.outcome == "kept"
    assert result.candidate_value == pytest.approx(0.404)
    assert result.improvement_pct == pytest.approx(0.01)


def test_discarded_on_relative_improvement_below_one_percent(tmp_path: Path):
    """Candidate = baseline * 1.005 must be discarded (under threshold)."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-discard",
        hypothesis={"kind": "rule_tweak", "text": "-"},
        experiment_fn=_fn_returns_0_402,
        baseline_value=0.40,
        keep_threshold=0.01,
        time_box_seconds=5.0,
    )
    assert result.outcome == "discarded"


def test_discarded_on_regression(tmp_path: Path):
    """A negative relative change is always discarded."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-regress",
        hypothesis={"kind": "rule_tweak", "text": "bad"},
        experiment_fn=_fn_returns_0_30,
        baseline_value=0.40,
        time_box_seconds=5.0,
    )
    assert result.outcome == "discarded"
    assert result.improvement_pct < 0


# ---------- time box ----------

def test_time_box_kills_long_running_experiment(tmp_path: Path):
    """An experiment_fn that exceeds time_box_seconds must raise ExperimentTimeout.

    With the R9 fix the child process is *terminated* on timeout, so
    the runner returns within ``time_box_seconds + grace``, not
    ``experiment_fn's total runtime``.
    """
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    start = time.time()

    with pytest.raises(ExperimentTimeout):
        run_single_experiment(
            profile_ini=ini,
            audit_path=audit,
            experiment_id="exp-timeout",
            hypothesis={"kind": "rule_tweak", "text": "slow"},
            experiment_fn=_slow_2s,
            baseline_value=0.40,
            time_box_seconds=0.2,
        )
    elapsed = time.time() - start
    # Time box should be respected; the test allows generous headroom
    # for child-process spawn + SIGTERM/SIGKILL grace on Windows.
    assert elapsed < 1.5, f"runner ignored time box: ran for {elapsed:.2f}s"


# ---------- circuit breaker ----------

def test_circuit_breaker_trips_on_fourth_experiment(tmp_path: Path):
    """code_patch_write budget is 3/hr; the 4th experiment in the same window must trip."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    cb = CircuitBreaker(root=tmp_path / "cb")

    for i in range(3):
        result = run_single_experiment(
            profile_ini=ini,
            audit_path=audit,
            experiment_id=f"exp-cb-{i}",
            hypothesis={"kind": "rule_tweak", "text": str(i)},
            experiment_fn=_fn_returns_0_50,
            baseline_value=0.40,
            time_box_seconds=5.0,
            circuit_breaker=cb,
        )
        assert result.outcome in {"kept", "discarded"}

    with pytest.raises(BudgetExceeded) as excinfo:
        run_single_experiment(
            profile_ini=ini,
            audit_path=audit,
            experiment_id="exp-cb-4",
            hypothesis={"kind": "rule_tweak", "text": "trip"},
            experiment_fn=_fn_returns_0_50,
            baseline_value=0.40,
            time_box_seconds=5.0,
            circuit_breaker=cb,
        )
    assert excinfo.value.action_class == "code_patch_write"


# ---------- audit trail ----------

def test_audit_row_appended_on_kept_outcome(tmp_path: Path):
    """A successful kept experiment must append exactly one audit row with action_class=code_patch_write."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-audit-keep",
        hypothesis={"kind": "rule_tweak", "text": "+"},
        experiment_fn=_fn_returns_0_50,
        baseline_value=0.40,
        time_box_seconds=5.0,
    )
    assert result.outcome == "kept"
    log = AuditLog(audit)
    rows = log.read_all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload.get("action_class") == "code_patch_write"
    assert payload.get("outcome") == "kept"
    assert payload.get("experiment_id") == "exp-audit-keep"


def test_audit_chain_verifies_after_experiment(tmp_path: Path):
    """The audit chain must remain valid after an experiment run."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    for i in range(3):
        run_single_experiment(
            profile_ini=ini,
            audit_path=audit,
            experiment_id=f"exp-chain-{i}",
            hypothesis={"kind": "rule_tweak", "text": str(i)},
            experiment_fn=_fn_returns_0_41,
            baseline_value=0.40,
            time_box_seconds=5.0,
        )
    log = AuditLog(audit)
    log.verify()  # must not raise
    rows = log.read_all()
    assert len(rows) == 3
    parsed = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    # Every row carries action_class=code_patch_write.
    assert all(r.get("action_class") == "code_patch_write" for r in parsed)


# ---------- result shape ----------

def test_result_carries_metric_metadata(tmp_path: Path):
    """The result must expose the metric name, baseline, candidate, and timestamps."""
    ini = _write_learning_profile(tmp_path)
    audit = _new_audit_path(tmp_path)
    result = run_single_experiment(
        profile_ini=ini,
        audit_path=audit,
        experiment_id="exp-meta",
        hypothesis={"kind": "rule_tweak", "text": "x"},
        experiment_fn=_fn_returns_0_42,
        baseline_value=0.40,
        metric_name="recall_view.hit_at_1",
        time_box_seconds=5.0,
    )
    assert result.metric_name == "recall_view.hit_at_1"
    assert result.baseline_value == pytest.approx(0.40)
    assert result.candidate_value == pytest.approx(0.42)
    assert result.started_at and result.finished_at
    assert result.duration_seconds >= 0.0
