# Autonomous Learning Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep unavailable replay evidence and noisy terminal text out of capability scores and autonomous learning.

**Architecture:** Preserve replay diagnostics while separating `not_run` from executed failures. Bind acceptance evidence in the nightly loop and add a terminal input quality gate before correction/outcome learning.

**Tech Stack:** Python 3.11+, pytest, SQLite-backed eimemory runtime, systemd user service.

## Global Constraints

- Use TDD for every behavior change.
- Do not weaken contract-backed replay validation.
- Do not delete historical records; rebuild the effective ledger from corrected evidence semantics.
- Bump the patch version and complete commit, push, immutable release deployment, restart, and health verification.

---

### Task 1: Correct replay score semantics

**Files:** `tests/test_capability_replay_packs.py`, `eimemory/governance/capability_replay_packs.py`

- [ ] Write a failing test proving all-`not_run` packs emit no score record and do not overwrite a prior ledger score.
- [ ] Run the test and confirm the current zero-score behavior fails it.
- [ ] Compute pass rate from executed cases only and skip score persistence when none executed.
- [ ] Run replay-pack tests.

### Task 2: Bind acceptance evidence in autonomous learning

**Files:** `tests/test_full_autonomous_learning_loop.py`, `eimemory/governance/autonomous_learning.py`

- [ ] Write a failing test asserting acceptance executes before replay and its identifiers are bound.
- [ ] Run the test and confirm missing binding.
- [ ] Add acceptance execution and pass its execution/probe IDs to replay.
- [ ] Run autonomous-learning tests.

### Task 3: Quarantine noisy terminal input

**Files:** `tests/test_openclaw_outcome_hooks.py`, `tests/test_learning_report.py`, `eimemory/adapters/openclaw/hooks.py`, `eimemory/governance/learning_report.py`

- [ ] Write failing mixed-transcript/voice-noise tests.
- [ ] Confirm the current hook records false corrections.
- [ ] Add a shared conservative quality classification and diagnostic-only path.
- [ ] Verify clean explicit corrections remain learnable.

### Task 4: Release and production verification

**Files:** `pyproject.toml`, `eimemory/version.py`

- [ ] Run targeted and full tests.
- [ ] Bump patch version.
- [ ] Review the diff and run code review.
- [ ] Commit and push.
- [ ] Deploy immutable release, restart the user service, and verify `/health` version/commit.
- [ ] Run a non-persisting or isolated acceptance/replay verification and confirm `not_run` cannot lower the ledger.
