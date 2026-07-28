# Eimemory Audit Closure Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair every concrete defect confirmed by the full-project and deployment-flow audits, then verify once and complete production business closure.

**Architecture:** Keep deployment rollback limited to artifact, migration, runtime identity, service startup, and health failures. Move recall, replay, channel delivery, lineage, and L5 evidence into a non-rollback post-deploy validation phase. Bind OpenClaw channel lineage to an actual platform-accepted Feishu receipt produced by the deployed commit.

**Tech Stack:** Python 3, SQLite, Bash, Node.js OpenClaw bridge, pytest, RTK.

## Global Constraints

- Make all source and test changes before running any test command.
- Preserve unrelated dirty-worktree changes.
- Do not expose API keys, platform identifiers, message contents, or raw delivery receipts.
- Keep Linux filesystem-contract tests; execute them in a Linux validation lane instead of deleting them.
- A deployment is technically successful only when commit/version/import-root identity and health agree.
- Business closure remains incomplete until real-query, replay, real-channel delivery, lineage, and readiness evidence agree.

---

### Task 1: Atomic scoped-storage migration

**Files:**
- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: legacy `records` table without `storage_key`.
- Produces: all-or-nothing migration; rollback restores the legacy table and rows after any exception.

- [x] Add `BEGIN IMMEDIATE`, `commit`, and exception rollback around rename/create/copy/drop.
- [x] Add failure-injection coverage that raises during row conversion, reopens the database, and proves every legacy row remains visible.

### Task 2: Sandbox and integration metadata consistency

**Files:**
- Modify: `eimemory/governance/code_evolution.py`
- Modify: `tests/test_code_evolution_sandbox.py`
- Modify: `.gitignore`
- Modify: `integrations/codex/eimemory/.codex-plugin/plugin.json`
- Modify: `integrations/hermes/eimemory/plugin.yaml`

**Interfaces:**
- Consumes: repository working directory containing `.worktrees`.
- Produces: bounded sandbox copy without recursive worktree content and integration versions equal to `eimemory.version.__version__`.

- [x] Exclude `.worktrees` and `.code-review-graph` from sandbox copies.
- [x] Extend the sandbox test to prove ignored control directories are absent.
- [x] Preserve existing ignore entries and add `.worktrees/`.
- [x] Set Codex and Hermes manifests to version `1.9.106`.

### Task 3: Real OpenClaw channel-delivery evidence

**Files:**
- Create: `eimemory/governance/openclaw_channel_acceptance.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/governance/release_lineage.py`
- Modify: `eimemory/ops/feishu_delivery_state.py`
- Modify: `eimemory/ops/openclaw_feishu_reply_watchdog.py`
- Modify: `integrations/openclaw/eimemory-bridge/index.js`
- Create: `tests/test_openclaw_channel_acceptance.py`
- Modify: `tests/test_release_closure.py`
- Modify: `tests/test_release_lineage.py`
- Modify: `tests/test_openclaw_reply_delivery_tracker.py`

**Interfaces:**
- Consumes: a `platform_accepted` delivery entry with a platform receipt and the deployed 40-character runtime commit.
- Produces: a redacted `eimemory.openclaw.channel_acceptance` learning record bound to the deployment receipt and release session.

- [x] Persist `runtime_commit` when the bridge or watchdog completes a real delivery.
- [x] Validate the delivery state strictly and persist only hashes/timestamps, never raw IDs or message text.
- [x] Add a Runtime facade for recording channel acceptance.
- [x] Add channel acceptance as a distinct release-closure stage.
- [x] Authorize channel lineage only from the new external-channel evidence contract; retain local live acceptance for storage integrity only.
- [x] Cover valid, stale-commit, pre-deployment, missing-state, malformed-state, and lineage-contract cases.

### Task 4: Deployment gate reordering

**Files:**
- Modify: `deploy/install_immutable_release.sh`
- Modify: `tests/test_deploy_loop_integration.py`
- Modify: `tests/test_deployment_tools.py`
- Modify: `tests/test_production_recall_bootstrap_deploy.py`
- Modify: `tests/test_storage_deploy.py`

**Interfaces:**
- Consumes: a full commit SHA and immutable archived source.
- Produces: a fast technical deployment transaction followed by non-rollback business validation.

- [x] Limit repository cleanliness checks to tracked deployment-control files that can affect the target archive.
- [x] Remove per-deploy pip self-upgrade; add `compileall` and `pip check`.
- [x] Move OpenClaw runtime verification after switch and service restart.
- [x] Run storage writer stop/snapshot/migration only for pending storage migrations.
- [x] Remove production recall bootstrap and prior-receipt requirements from pre-switch storage preparation.
- [x] Perform one final health verification.
- [x] Mark the deployment committed after technical health succeeds.
- [x] Run deployment receipt, lineage, recall/replay/live/channel/L5 closure afterward as retryable non-rollback validation.
- [x] Make an already-current invocation a cheap identity/health check without restart or full closure.

### Task 5: Test hygiene

**Files:**
- Delete: `tests/test_proposal_card_enforcement.py`
- Modify: `tests/test_deployment_tools.py`
- Create: `.github/workflows/linux-deployment-contracts.yml`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: Windows developer runs and Linux production-contract runs.
- Produces: no stale opt-in duplicate, semantic deployment assertions, and mandatory Linux execution for POSIX contracts.

- [x] Delete the permanently skipped duplicate ProposalCard test.
- [x] Replace exact `--scope-user` occurrence counting with checks on the specific receipt, lineage, health, and closure commands.
- [x] Register a `linux_deployment` marker and mark the POSIX deployment modules.
- [x] Add a Linux workflow that executes the deployment/storage/security contract suites without skipping Linux semantics.

### Task 6: Console fail-closed hardening

**Files:**
- Modify: `eimemory/governance/serve_console.py`
- Create: `tests/test_serve_console.py`

**Interfaces:**
- Consumes: a configured console token and tokenized request path.
- Produces: fail-closed access, constant-time token comparison, and non-leaking browser security headers.

- [x] Reject all requests when the console token is missing.
- [x] Hide incorrect token paths behind a 404 response.
- [x] Add no-store, no-referrer, nosniff, frame, and content-security headers.
- [x] Cover missing, incorrect, and exact-token access.

### Task 7: Linux deployment transaction closure

**Files:**
- Modify: `deploy/storage_release_transaction.py`
- Modify: `tests/test_storage_deploy.py`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Consumes: concurrent POSIX transaction starts, durable-clear failures, and symlinked lock attacks.
- Produces: one transaction winner, fail-closed durable credentials, normalized security errors, and environment-independent installer harnesses.

- [x] Tolerate a peer creating the marker parent while preserving fd/inode validation.
- [x] Normalize `O_NOFOLLOW` symlink failures into the storage transaction security contract.
- [x] Exercise the real parent-sync abstraction for clear durability failure injection.
- [x] Keep recovery-file fsync injection isolated from directory fsync.
- [x] Make Linux installer tests use the actual runner user and group.
- [x] Account for the installer lock acquisition receipt before the harness-ready line.

### Task 8: Single verification and production closure

**Files:**
- Verify all modified files.

**Interfaces:**
- Consumes: completed implementation.
- Produces: one coherent local verification record and, if remote access is available, one deployed production closure record.

- [x] Run RTK-focused tests for every changed subsystem.
- [x] Run RTK full pytest exactly once.
- [x] Run compileall, `git diff --check`, version checks, and deployment-script syntax validation.
- [ ] Deploy the verified commit from `/dev-project/eimemory`.
- [ ] Verify `/health`, commit/version/import root, services, real recall, replay, real channel receipt, lineage, and readiness.
