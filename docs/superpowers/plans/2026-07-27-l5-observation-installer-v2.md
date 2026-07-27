# L5 Observation Installer V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every immutable deployment replace and enable the packaged L5 observation gate script, service, and timer so production cannot retain stale user-systemd files.

**Architecture:** Extend the existing candidate-runtime metadata transaction with one focused installer helper. The helper installs all three files from the candidate immutable release, reloads user systemd, and enables the timer; rollback/current-release refresh uses the same helper through `_install_current_runtime_metadata`.

**Tech Stack:** Bash, systemd user units, Python 3.11+, pytest, Git immutable releases.

## Global Constraints

- Install only from the immutable candidate/current release, never the mutable source checkout.
- Preserve the canonical 48-hour first observation and six-hour recheck timer contract.
- Do not manufacture L5 evidence; production closure must use existing records and real queries only.
- Release version is `1.9.95`.

---

### Task 1: Regression test and installer fix

**Files:**
- Modify: `tests/test_deployment_tools.py`
- Modify: `deploy/install_immutable_release.sh`

**Interfaces:**
- Produces: `_install_l5_observation_gate <release-dir>`.
- Installs: `eimemory-l5-observation-gate.sh`, `.service`, and `.timer` under `$USER_SYSTEMD_DIR`.

- [ ] Add a test requiring all three candidate-release copies, executable mode for the script, daemon reload, and timer enablement.
- [ ] Run `pytest -q tests/test_deployment_tools.py -k immutable_installer_refreshes_l5_observation_gate` and confirm it fails because the installer omits the files.
- [ ] Add the minimal helper and call it from `_install_current_runtime_metadata`.
- [ ] Re-run the selected test and adjacent deployment tests.

### Task 2: Version and repository verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

- [ ] Bump both version declarations to `1.9.95`.
- [ ] Run focused deployment/version tests, compileall, shell syntax, `git diff --check`, then the full suite.
- [ ] Commit and push `fix/l5-observation-installer-v2`.

### Task 3: Immutable production repair

**Files:**
- Runtime output only under `/opt/eimemory/releases/<commit>` and `~/.config/systemd/user`.

- [ ] Fast-forward canonical `master`, push it, and run the official immutable installer for the full commit.
- [ ] Verify the active release, user units, timer properties, checksums, `/health`, and `learn l5-readiness --json`.
- [ ] Rebuild lineage/replay/assessment/strict-recall evidence only through official commands over real production records and queries.
- [ ] Write `/tmp/eimemory-l5-repair-report.md` with exact evidence and zero-or-explicit `known_fixable_issues` / `verification_gaps`.
