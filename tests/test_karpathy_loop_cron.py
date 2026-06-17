"""Tests for the Karpathy Loop nightly cron wrapper (Task 2.5).

The cron script is the nightly entry point that drives the autoresearch
loop unattended. The hard requirements pinned by these tests mirror
``docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md`` Task 2.5:

1. The script lives at ``scripts/karpathy_loop_cron.sh`` under the repo root.
2. It exports ``EXP_BUDGET=50`` (one experiment per slot, 50 slots per night).
3. It exports ``TIME_BUDGET_SECONDS=14400`` (4 hours wall clock per night).
4. It writes kept-experiment rows to ``kept-YYYYMMDD.log`` in the exp_log dir.
5. It is syntactically valid bash (``bash -n`` succeeds; skipped if bash is
   not on PATH so Windows dev boxes without Git Bash still pass).
6. The systemd user-service and user-timer live in ``deploy/systemd/``.
7. The service unit's ``ExecStart`` references the cron script.
8. The timer unit uses ``OnCalendar`` so it can actually schedule.

Path resolution is repo-root relative — the test works on Linux, macOS,
and Windows. The shell script itself is Linux-only; the Windows dev box
is expected to commit it as-is with a comment header explaining why.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Repo root = parent of the ``tests/`` directory that contains this file.
REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_SCRIPT = REPO_ROOT / "scripts" / "karpathy_loop_cron.sh"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE_FILE = SYSTEMD_DIR / "eimemory-karpathy-loop.service"
TIMER_FILE = SYSTEMD_DIR / "eimemory-karpathy-loop.timer"


# ---------- helpers ----------

def _read(path: Path) -> str:
    """Read a file as UTF-8 text; fail loudly with a useful path on error."""
    assert path.exists(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


# ---------- script presence ----------

def test_cron_script_exists() -> None:
    """The nightly cron script must live at scripts/karpathy_loop_cron.sh."""
    assert CRON_SCRIPT.exists(), f"missing: {CRON_SCRIPT}"


def test_cron_script_has_bash_shebang() -> None:
    """The first line must be a bash shebang so cron/systemd can exec it."""
    first_line = CRON_SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), f"missing shebang: {first_line!r}"
    assert "bash" in first_line, f"shebang must reference bash, got: {first_line!r}"


# ---------- budget constants ----------

def test_cron_script_sets_exp_budget_50() -> None:
    """The plan requires EXP_BUDGET=50 (one experiment per slot, 50 per night)."""
    content = _read(CRON_SCRIPT)
    # Match ``EXP_BUDGET=50`` or ``EXP_BUDGET = 50``; reject "=500", "=5", etc.
    assert re.search(r"^\s*EXP_BUDGET\s*=\s*50\s*$", content, re.MULTILINE), (
        "EXP_BUDGET=50 (50 experiments per night) is required by the Phase 2 plan"
    )


def test_cron_script_sets_time_budget_14400() -> None:
    """The plan requires TIME_BUDGET_SECONDS=14400 (4h wall clock per night)."""
    content = _read(CRON_SCRIPT)
    assert re.search(
        r"^\s*TIME_BUDGET_SECONDS\s*=\s*14400\s*$", content, re.MULTILINE
    ), "TIME_BUDGET_SECONDS=14400 (4 hours per night) is required by the Phase 2 plan"


# ---------- output log ----------

def test_cron_script_writes_kept_log() -> None:
    """The script must write kept experiments to kept-YYYYMMDD.log (date-stamped)."""
    content = _read(CRON_SCRIPT)
    # Look for the literal "kept-" token AND a ".log" token; the date
    # formatter (``$(date +%Y%m%d)`` or ``${DATE_STAMP}``) is allowed to vary.
    assert "kept-" in content, "expected 'kept-' token in log path"
    assert ".log" in content, "expected '.log' suffix in log path"
    # A date stamp must appear in the log path. Accept either:
    #   - shell ``$(date +%Y%m%d)`` (most common)
    #   - a DATE_STAMP / YYYYMMDD-style variable
    has_date_subst = bool(
        re.search(r"kept-.*\$\(date", content)
        or re.search(r"kept-.*\$\{?DATE", content, re.IGNORECASE)
        or re.search(r"kept-.*%Y%m%d", content)
        or re.search(r"kept-[A-Z_]*\$\{?date", content, re.IGNORECASE)
    )
    assert has_date_subst, "kept- log path must include a date stamp substitution"


# ---------- syntax check (skipped without bash) ----------

def _find_real_bash() -> str | None:
    """Locate a bash that actually understands ``-n`` syntax checks.

    On Windows the system ``bash.exe`` is often the WSL launcher
    (which prints a help string and exits 1) rather than GNU bash. We
    prefer the Git Bash path if it exists, then fall back to ``bash``
    on PATH. We verify the candidate by running ``-n`` against a
    no-op heredoc; if that succeeds the binary is real bash.
    """
    import pytest

    candidates: list[str] = []
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if git_bash.exists():
        candidates.append(str(git_bash))
    on_path = shutil.which("bash")
    if on_path:
        candidates.append(on_path)
    for cand in candidates:
        probe = subprocess.run(
            [cand, "-n", "-c", "true"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return cand
    return None


def test_cron_script_is_bash_valid() -> None:
    """``bash -n`` must accept the script; skip the test if bash is absent."""
    import pytest

    bash = _find_real_bash()
    if not bash:
        pytest.skip("bash not on PATH (Windows dev box without Git Bash)")
    result = subprocess.run(
        [bash, "-n", str(CRON_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n failed: stderr={result.stderr!r} stdout={result.stdout!r}"
    )


# ---------- systemd unit files ----------

def test_systemd_service_unit_exists() -> None:
    """The systemd service unit must live at deploy/systemd/."""
    assert SERVICE_FILE.exists(), f"missing: {SERVICE_FILE}"


def test_systemd_timer_unit_exists() -> None:
    """The systemd timer unit must live at deploy/systemd/."""
    assert TIMER_FILE.exists(), f"missing: {TIMER_FILE}"


def test_systemd_service_runs_the_cron_script() -> None:
    """The service unit's ExecStart must reference karpathy_loop_cron.sh."""
    content = _read(SERVICE_FILE)
    assert "ExecStart" in content, "service unit missing ExecStart"
    assert "karpathy_loop_cron" in content, (
        "ExecStart must reference the karpathy_loop_cron.sh script"
    )


def test_systemd_service_uses_oneshot_type() -> None:
    """The service must be Type=oneshot (a single nightly run, not a daemon)."""
    content = _read(SERVICE_FILE)
    assert re.search(r"^\s*Type\s*=\s*oneshot\s*$", content, re.MULTILINE), (
        "service must be Type=oneshot; the cron tick is a single run, not a daemon"
    )


def test_systemd_timer_uses_on_calendar() -> None:
    """The timer must use OnCalendar so systemd can schedule it nightly."""
    content = _read(TIMER_FILE)
    assert re.search(r"^\s*OnCalendar\s*=", content, re.MULTILINE), (
        "timer must set OnCalendar; nightly schedule is the whole point"
    )


def test_systemd_timer_references_service() -> None:
    """The timer must name the service unit it triggers."""
    content = _read(TIMER_FILE)
    assert "eimemory-karpathy-loop.service" in content, (
        "timer must reference eimemory-karpathy-loop.service"
    )
