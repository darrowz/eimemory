#!/usr/bin/env bash
# karpathy_loop_cron.sh — nightly autoresearch loop driver (Task 2.5)
#
# This script is the nightly entry point for the Karpathy Loop. It is
# invoked once per night by the eimemory-karpathy-loop.service systemd
# user unit (or directly by cron) and runs up to EXP_BUDGET=50 single
# experiments under a hard TIME_BUDGET_SECONDS=14400 (4 hours) wall-clock
# budget. Each kept experiment is appended to kept-YYYYMMDD.log in the
# exp_log directory so the next iteration can use it as compounding
# context.
#
# Phase 2 plan reference:
#   docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md
#   Task 2.5 (cron wrapper + systemd timer, 50 exp / 4h nightly).
#
# This script is **Linux-only**. It uses bash + date(1) + python3. The
# Windows dev box commits it as-is so the production Linux deployment
# has the canonical wrapper; do not try to run it on Windows.
#
# Required environment:
#   EIMEMORY_ROOT   - state directory (e.g. /var/lib/eimemory). Used to
#                     locate audit.jsonl, profile ini, and the exp_log
#                     directory.
#   EIMEMORY_CONFIG_DIR - config directory (e.g. /etc/eimemory). Used to
#                     locate eimemory.ini (the autonomy profile).
#
# Behavioural contract (pinned by tests/test_karpathy_loop_cron.py):
#   - EXP_BUDGET=50
#   - TIME_BUDGET_SECONDS=14400
#   - kept experiments appended to $EIMEMORY_ROOT/exp_log/kept-YYYYMMDD.log
#   - bash -n must accept this file (no syntax errors)
#   - runnable as a systemd Type=oneshot service
#
# Exit codes:
#   0  - normal completion (budget exhausted or all experiments ran)
#   1  - configuration error (EIMEMORY_ROOT not set, etc.)
#   2  - profile gate refused Phase 2 work
#   3  - circuit breaker tripped mid-run (not fatal; remaining budget
#        is left for tomorrow)
#
# This script is conservative: it never rm -rfs, never pushes, never
# reaches out to the network, never edits code. It only orchestrates
# the eimemory.autonomous.loop module, which is the gated runner.
set -euo pipefail

# ---- Budget constants (pinned by tests/test_karpathy_loop_cron.py) ----
EXP_BUDGET=50
TIME_BUDGET_SECONDS=14400

# ---- Environment guards ----
if [[ -z "${EIMEMORY_ROOT:-}" ]]; then
    echo "karpathy_loop_cron: EIMEMORY_ROOT is not set" >&2
    exit 1
fi

PROFILE_INI="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}/eimemory.ini"
AUDIT_LOG="${EIMEMORY_ROOT}/audit.jsonl"
EXP_LOG_DIR="${EIMEMORY_ROOT}/exp_log"
DATE_STAMP="$(date -u +%Y%m%d)"
KEPT_LOG="${EXP_LOG_DIR}/kept-${DATE_STAMP}.log"
EXP_JSONL="${EXP_LOG_DIR}/experiments.jsonl"
RECORDS_PATH="${EIMEMORY_ROOT}/records.jsonl"

mkdir -p "${EXP_LOG_DIR}"

# ---- Profile gate (fail fast before the time budget starts) ----
# Reads eimemory.ini via the governance safety profile module. If the
# profile is conservative the script exits 2 (the budget must not be
# spent on a profile that blocks Phase 2 work).
if ! PROFILE_OUT="$(python3 - "$PROFILE_INI" <<'PYEOF'
import sys
from pathlib import Path
from eimemory.governance.safety.profile import load_profile
profile = load_profile(Path(sys.argv[1]))
if not profile.can_run_phase2():
    raise SystemExit(2)
print(profile.profile.value)
PYEOF
)"; then
    rc=$?
    if [[ $rc -eq 2 ]]; then
        echo "karpathy_loop_cron: profile=conservative blocks Phase 2; exiting" >&2
        exit 2
    fi
    echo "karpathy_loop_cron: profile check failed (rc=$rc)" >&2
    exit 1
fi

# ---- Loop body ----
START_EPOCH="$(date +%s)"
EXPERIMENTS_RUN=0
KEEP_COUNT=0
DISCARD_COUNT=0
TIMEOUT_COUNT=0

for ((i = 1; i <= EXP_BUDGET; i++)); do
    NOW_EPOCH="$(date +%s)"
    ELAPSED=$((NOW_EPOCH - START_EPOCH))
    if (( ELAPSED >= TIME_BUDGET_SECONDS )); then
        echo "karpathy_loop_cron: time budget exhausted after ${ELAPSED}s" >&2
        break
    fi

    EXPERIMENT_ID="kl-$(date -u +%Y%m%dT%H%M%S)-${i}"

    # Run a single real hypothesis-driven iteration. The runner owns
    # hypothesis selection, scoring, audit, and exp_log append; this
    # wrapper only enforces nightly budgets and kept/discard accounting.
    if EXPERIMENT_JSON="$(python3 - "$PROFILE_INI" "$AUDIT_LOG" "$RECORDS_PATH" "$EXP_JSONL" "$EXPERIMENT_ID" <<'PYEOF'
import json
import sys
from pathlib import Path

from eimemory.autonomous.loop import (
    ExperimentTimeout,
    ProfileBlocked,
)
from eimemory.autonomous.runner import run_karpathy_iteration
from eimemory.governance.safety.circuit_breaker import BudgetExceeded
from eimemory.governance.safety.profile import load_profile

profile_ini = Path(sys.argv[1])
audit_path = Path(sys.argv[2])
records_path = Path(sys.argv[3])
exp_log_path = Path(sys.argv[4])
experiment_id = sys.argv[5]

# Re-check the profile gate here too: if the profile flipped between
# the outer check and this experiment, the runner will refuse; we
# surface that as a non-zero rc and let the wrapper log it.
profile = load_profile(profile_ini)
if not profile.can_run_phase2():
    raise SystemExit(2)


try:
    result = run_karpathy_iteration(
        profile_ini=profile_ini,
        audit_path=audit_path,
        records_path=records_path,
        exp_log_path=exp_log_path,
        experiment_id=experiment_id,
        time_box_seconds=300.0,
    )
except ExperimentTimeout:
    raise SystemExit(4)
except BudgetExceeded:
    raise SystemExit(3)
except ProfileBlocked:
    raise SystemExit(2)

print(json.dumps(result, default=str))
PYEOF
)"; then
        OUTCOME="$(printf '%s' "$EXPERIMENT_JSON" | python3 -c "
import json, sys
rec = json.loads(sys.stdin.read())
print(rec.get('outcome', 'unknown'))
")"
        case "${OUTCOME}" in
            kept)
                KEEP_COUNT=$((KEEP_COUNT + 1))
                printf '%s\n' "$EXPERIMENT_JSON" >> "${KEPT_LOG}"
                ;;
            timeout)
                TIMEOUT_COUNT=$((TIMEOUT_COUNT + 1))
                ;;
            *)
                DISCARD_COUNT=$((DISCARD_COUNT + 1))
                ;;
        esac
    else
        inner_rc=$?
        case $inner_rc in
            3)
                # Circuit-breaker tripped; recoverable — exit 0 because
                # the script did its job, the next run gets a fresh
                # hourly budget.
                echo "karpathy_loop_cron: circuit breaker tripped at i=${i}" >&2
                break
                ;;
            4)
                TIMEOUT_COUNT=$((TIMEOUT_COUNT + 1))
                ;;
            2)
                # Profile flipped to conservative mid-run. Treat the
                # same as the outer gate failure.
                echo "karpathy_loop_cron: profile flipped mid-run; exiting" >&2
                exit 2
                ;;
            *)
                # Treat any other error as a discard; do not abort the
                # run for a single bad experiment.
                DISCARD_COUNT=$((DISCARD_COUNT + 1))
                ;;
        esac
    fi
    EXPERIMENTS_RUN=$((EXPERIMENTS_RUN + 1))
done

END_EPOCH="$(date +%s)"
ELAPSED_TOTAL=$((END_EPOCH - START_EPOCH))

echo "karpathy_loop_cron: ran=${EXPERIMENTS_RUN} kept=${KEEP_COUNT} " \
    "discarded=${DISCARD_COUNT} timeout=${TIMEOUT_COUNT} " \
    "elapsed=${ELAPSED_TOTAL}s log=${KEPT_LOG}"
exit 0
