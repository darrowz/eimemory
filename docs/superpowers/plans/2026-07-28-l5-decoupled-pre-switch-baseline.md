# L5-Decoupled Pre-switch Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore trustworthy pre-switch recall baselines while making L5 availability and evidence authority independent from core deployment success and package version numbers.

**Architecture:** The immutable installer performs a best-effort, non-blocking L5 baseline observation while the predecessor is still live, then completes the existing technical transaction independently. L5 evidence uses commit, verified deployment receipt, and release session as its authority key; version remains serialized metadata but is excluded from L5 equality, freshness, lineage, channel acceptance, and promotion decisions.

**Tech Stack:** Bash, Python 3.14, pytest, SQLite-backed `RuntimeStore`, systemd user services, RTK, GitHub Actions Ubuntu deployment contracts.

## Global Constraints

- Core deployment blockers are limited to release construction, dependency integrity, source/runtime identity, storage migration safety, service restart, and health verification.
- L5 baseline, replay, live acceptance, channel acceptance, closure rehearsal, and readiness failures must never change core installer success, `/health`, RPC, storage, recall, or OpenClaw availability.
- L5 authority is `(commit, deployment_receipt_id, release_session_id)`; package version is descriptive metadata only.
- Same-version commits remain distinct authorities; changing only a version never creates or invalidates L5 evidence.
- Do not weaken commit, receipt, session, scope, record-integrity, or anti-self-blessing checks.
- Do not bump version `1.9.106` for this work.
- Preserve the user's unrelated deleted audit documents and untracked `CODE_AUDIT_REPORT.md`.
- Write all regression tests first, run one combined RED batch, complete all production edits, then run one combined GREEN batch. Do not rerun the full local suite.

---

### Task 1: Add all failing L5 isolation regressions

**Files:**
- Modify: `tests/test_governance_evidence_contract.py`
- Modify: `tests/test_openclaw_channel_acceptance.py`
- Modify: `tests/test_live_task_acceptance.py`
- Modify: `tests/test_release_lineage.py`
- Modify: `tests/test_l5_readiness.py`
- Modify: `tests/test_l5_closure_rehearsal.py`
- Modify: `tests/test_production_real_query_gate.py`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Consumes: existing `ReleaseIdentity`, evidence resolution, deployment receipts, release lineage, live/channel acceptance, recall gate, and immutable installer.
- Produces: regression expectations for `same_release_authority(...)` and optional pre-switch baseline behavior.

- [x] **Step 1: Add version-neutral authority tests**

Add literal authority fixtures whose commit, receipt, and session match while versions differ:

```python
expected = ReleaseIdentity("a" * 40, "1.9.106", "receipt-a", "session-a")
descriptive_drift = ReleaseIdentity("a" * 40, "9.9.999", "receipt-a", "session-a")

assert same_release_authority(expected, descriptive_drift) is True
assert same_release_authority(
    expected,
    ReleaseIdentity("b" * 40, "1.9.106", "receipt-a", "session-a"),
) is False
assert same_release_authority(
    expected,
    ReleaseIdentity("a" * 40, "1.9.106", "receipt-b", "session-a"),
) is False
assert same_release_authority(
    expected,
    ReleaseIdentity("a" * 40, "1.9.106", "receipt-a", "session-b"),
) is False
```

Exercise real consumers rather than asserting source text:

- `resolve_evidence` accepts descriptive version drift and rejects commit, receipt, or session drift.
- `current_release_identity` selects the verified receipt by runtime commit even when its descriptive version differs from imported `__version__`.
- OpenClaw channel acceptance verifies commit/receipt/session and ignores `deployment_version` drift.
- live task acceptance, release lineage, closure rehearsal, real-query verification, and L5 readiness remain valid when only serialized version metadata differs.

- [x] **Step 2: Add installer behavior harness**

Run the real `_capture_prior_health_snapshot` and `_run_pre_switch_production_recall_bootstrap` function bodies in a temporary Bash harness with stubbed health capture, service-user execution, and bootstrap exit statuses.

Assert these literal outcomes:

```python
assert result.returncode == 0
assert "l5_pre_switch_bootstrap=ready" in success.stdout
assert "l5_pre_switch_bootstrap=degraded exit_status=1" in business_failure.stderr
assert "l5_pre_switch_bootstrap=error exit_status=2" in code_failure.stderr
assert not snapshot_path.exists()
```

Run a temporary full-installer fixture with no pending storage migration and verify the current link still switches when the L5 bootstrap exits `1` or `2`.

- [x] **Step 3: Run one combined RED batch**

Run:

```powershell
& 'C:\Users\maiph\.rtk\bin\rtk.exe' pytest -q --strict-markers `
  tests/test_governance_evidence_contract.py `
  tests/test_openclaw_channel_acceptance.py `
  tests/test_live_task_acceptance.py `
  tests/test_release_lineage.py `
  tests/test_l5_readiness.py `
  tests/test_l5_closure_rehearsal.py `
  tests/test_production_real_query_gate.py `
  tests/test_deployment_tools.py
```

Expected: only the newly added tests fail, specifically because version is still part of dataclass equality/current-release selection and the installer has no pre-switch bootstrap functions.

---

### Task 2: Make L5 evidence authority version-neutral

**Files:**
- Modify: `eimemory/governance/evidence_contract.py`

**Interfaces:**
- Consumes: verified deployment receipt records.
- Produces:
  - `release_authority_key(release: ReleaseIdentity) -> tuple[str, str, str]`
  - `same_release_authority(left: ReleaseIdentity | None, right: ReleaseIdentity | None) -> bool`
  - `ReleaseIdentity.complete` independent from `version`

- [x] **Step 1: Add the authority-key helpers**

Implement:

```python
def release_authority_key(release: ReleaseIdentity) -> tuple[str, str, str]:
    return (
        str(release.commit or "").strip().lower(),
        str(release.receipt_id or "").strip(),
        str(release.session_id or "").strip(),
    )


def same_release_authority(
    left: ReleaseIdentity | None,
    right: ReleaseIdentity | None,
) -> bool:
    return bool(
        isinstance(left, ReleaseIdentity)
        and isinstance(right, ReleaseIdentity)
        and left.complete
        and right.complete
        and release_authority_key(left) == release_authority_key(right)
    )
```

Change `ReleaseIdentity.complete` to require a 40-character commit, receipt, and session, but not `version`.

- [x] **Step 2: Apply authority semantics at the shared boundary**

In `resolve_evidence`, replace dataclass equality with `same_release_authority(actual, release)`.

In `current_release_identity`, retain runtime-commit and verified-receipt validation but remove the comparison between receipt version and imported `eimemory.__version__`.

Keep `_verified_receipt_identity`'s internal deployment-health version consistency check unchanged because it validates the core deployment receipt rather than L5 authority.

---

### Task 3: Remove version equality from every L5 consumer

**Files:**
- Modify: `eimemory/evaluation/real_query_gate.py`
- Modify: `eimemory/governance/openclaw_channel_acceptance.py`
- Modify: `eimemory/governance/live_task_acceptance.py`
- Modify: `eimemory/governance/release_lineage.py`
- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `eimemory/governance/closure_rehearsal.py`
- Modify: `deploy/summarize_release_closure.py`
- Modify: `eimemory/adapters/runtime/service.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/retrieval/proactive.py`
- Modify: `eimemory/storage/runtime_store.py`

**Interfaces:**
- Consumes: `same_release_authority(...)` from Task 2.
- Produces: L5 recall, channel, live-task, lineage, rehearsal, and readiness decisions keyed only by commit/receipt/session.

- [x] **Step 1: Replace L5 identity equality**

Import `same_release_authority` and replace direct `ReleaseIdentity` equality in:

- real-query report, baseline, and current-release comparisons;
- release-lineage current/ancestor/evidence-release comparisons;
- closure rehearsal lineage checks;
- L5 readiness current/evidence release checks.

Version fields remain in JSON payloads for observability but are not compared.

- [x] **Step 2: Remove version gates from channel and live acceptance**

For OpenClaw channel acceptance, require:

```python
same_release_authority(recorded_release, current_release)
```

Do not require `deployment_version == current_release.version`.

For live task acceptance, derive authority from the verified deployment receipt and require commit, receipt, session, release path, and post-deployment time ordering. Keep the reported version as metadata, but remove `version == __version__` and version-present conditions from L5 pass/fail.

- [x] **Step 3: Keep core deployment checks intact**

Do not change:

- `deploy/verify_release_health.py`;
- package version agreement in the deployment receipt;
- `/health` version reporting;
- immutable release path and commit verification.

This preserves core release integrity while preventing those fields from becoming L5 promotion keys.

---

### Task 4: Restore optional pre-switch baseline capture

**Files:**
- Modify: `deploy/install_immutable_release.sh`

**Interfaces:**
- Consumes:
  - `deploy/capture_prior_health_snapshot.py`
  - `deploy/bootstrap_production_recall.py`
  - existing `PREVIOUS_COMMIT`, `CURRENT_LINK`, candidate `RELEASE_DIR`, and governance scope variables
- Produces: best-effort predecessor baseline evidence and bounded L5 status markers.

- [x] **Step 1: Restore protected snapshot lifecycle**

Add `PRIOR_HEALTH_SNAPSHOT_FILE=""` to installer state and restore `_capture_prior_health_snapshot()` using:

```bash
snapshot_file="$(mktemp "$INSTALL_ROOT/.prior-health-${COMMIT}-XXXXXXXX.json")"
chmod 0600 "$snapshot_file"
"$RELEASE_DIR/.venv/bin/python" -I -B \
  "$RELEASE_DIR/deploy/capture_prior_health_snapshot.py" \
  --health-url "$EIMEMORY_HEALTH_URL" >"$snapshot_file"
```

Apply existing service-user ownership rules and remove the file from `cleanup_stage()` on every exit path.

- [x] **Step 2: Restore bootstrap invocation as an L5-only observer**

Call the existing helper as the service user with candidate commit, predecessor commit, protected health snapshot, root, and exact governance scope.

Classify its exit without propagating failure to the core installer:

```bash
case "$bootstrap_status" in
  0) echo "l5_pre_switch_bootstrap=ready" ;;
  1) echo "l5_pre_switch_bootstrap=degraded exit_status=1" >&2 ;;
  *) echo "l5_pre_switch_bootstrap=error exit_status=$bootstrap_status" >&2 ;;
esac
return 0
```

If health snapshot capture itself fails, emit
`l5_pre_switch_bootstrap=error stage=prior_health_capture` and continue without a snapshot.

- [x] **Step 3: Place the observer outside the storage transaction**

Invoke capture/bootstrap after candidate construction and configuration provisioning, before `_prepare_storage_for_release` and before the current symlink switch.

This ensures the predecessor RPC is still live, works for code-only deployments, and never adds storage snapshot work.

---

### Task 5: Run one GREEN verification batch and publish

**Files:**
- Modify: `docs/superpowers/plans/2026-07-28-l5-decoupled-pre-switch-baseline.md`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: local verification evidence, pushed commit, and mandatory Ubuntu CI evidence.

- [x] **Step 1: Run the focused GREEN batch once**

Run the same eight test files from Task 1 with RTK.

Expected: zero failures. Record the exact passed/skipped counts.

Recorded: new regression batch `16 passed`; expanded affected-area batch
`412 passed, 1 skipped` after removing one obsolete version-binding
expectation; governance summary rerun `37 passed, 1 skipped`.

- [x] **Step 2: Run static verification**

Run:

```powershell
& 'C:\Users\maiph\.rtk\bin\rtk.exe' proxy 'C:\Program Files\Git\bin\bash.exe' -n deploy/install_immutable_release.sh
python -m compileall -q eimemory deploy
& 'C:\Users\maiph\.rtk\bin\rtk.exe' git diff --check
```

Expected: all commands exit `0`.

Recorded: Bash syntax, repository Python AST parsing, and `git diff --check`
all exited `0`.

- [ ] **Step 3: Commit and push only scoped files**

Stage the plan, installer, the listed L5 consumers, shared evidence contract, real-query gate, and their tests. Do not stage the user's deleted audit documents or `CODE_AUDIT_REPORT.md`.

Commit:

```powershell
& 'C:\Users\maiph\.rtk\bin\rtk.exe' git commit -m "fix: decouple L5 release evidence"
& 'C:\Users\maiph\.rtk\bin\rtk.exe' git push origin master
```

- [ ] **Step 4: Wait for mandatory Ubuntu deployment contracts**

Require GitHub Actions `Linux deployment contracts` to complete with conclusion `success`. On failure, inspect the failing job log and fix only the demonstrated Linux contract.

---

### Task 6: Rebuild the predecessor baseline and finish production closure

**Files:**
- No repository file changes expected.
- Remote immutable releases: `/opt/eimemory/releases/<commit>`
- Remote current link: `/opt/eimemory/current`
- Authoritative repo: `/dev-project/eimemory`

**Interfaces:**
- Consumes: successful Task 5 commit and CI.
- Produces: deployed commit identity, predecessor recall baseline, independent L5 outcome, and core-function health evidence.

- [ ] **Step 1: Temporarily switch to the immutable predecessor**

On `darrow@honxin`, fetch and fast-forward `/dev-project/eimemory`, then run:

```bash
EIMEMORY_POST_SWITCH_GATES=0 \
  bash deploy/install_immutable_release.sh \
  a2c35601dcd4737ac315a1d6a355c9d4bbc27a6d
```

Verify `/opt/eimemory/current`, local `/health`, and all three user services report the predecessor commit and remain healthy.

- [ ] **Step 2: Deploy the fixed candidate**

Run `deploy/install_immutable_release.sh <full-fixed-commit>`. Confirm the output includes a pre-switch L5 marker and `commit_complete=1`.

- [ ] **Step 3: Verify core functions independently**

Require:

- `/health.ok == true`;
- health commit and configured runtime commit equal the fixed commit;
- current link resolves to `/opt/eimemory/releases/<fixed-commit>`;
- `eimemory-rpc`, `openclaw-gateway`, and `openclaw-loopback-proxy` are active;
- `systemctl --user --failed` is empty;
- one bounded recall/RPC probe succeeds.

These checks pass or fail independently from L5.

- [ ] **Step 4: Run strict L5 closure separately**

Run release closure, then independent `learn l5-readiness --persist`. Report:

- production recall baseline and gate;
- weak-capability replay;
- live task acceptance;
- OpenClaw channel acceptance;
- closure rehearsal;
- final readiness stage and score.

If any L5 stage remains blocked, preserve the healthy core deployment and report the exact L5 evidence blocker. Do not bump the package version and do not roll back core functionality.
