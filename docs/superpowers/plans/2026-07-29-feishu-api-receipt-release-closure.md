# Feishu API Receipt and Event-Driven Release Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist real Feishu automatic-reply API receipts and immediately resume the matching release closure through an independent systemd path unit.

**Architecture:** A managed, version-gated OpenClaw runtime patch emits the existing canonical `message_sent` plugin hook at the Feishu API success boundary. EIMemory persists a commit-bound pending checkpoint when channel evidence is the only missing gate, and a path-triggered oneshot validates the receipt and resumes only the post-channel closure stages.

**Tech Stack:** Python 3.11+, Node.js/OpenClaw plugin hooks, pytest, systemd user units, Bash immutable installer.

## Global Constraints

- A successful receipt requires `success=true` and a nonempty Feishu platform message ID.
- Do not accept `agent_end`, `dispatch complete`, or error absence as platform evidence.
- Do not restore or depend on `openclaw-feishu-reply-watchdog`.
- Do not bind release eligibility or L5 state to a semantic version.
- Do not reset, start, or downgrade L5 from the channel reconciliation path.
- Run only focused and adjacent tests; do not run the full project suite.
- Deploy from `/dev-project/eimemory` with a full 40-character commit and verify `/opt/eimemory/current` plus `/health`.

---

### Task 1: Managed OpenClaw Feishu Receipt Patch

**Files:**
- Create: `deploy/patch_openclaw_feishu_api_receipt.py`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`
- Test: `tests/test_deployment_tools.py`
- Test: `tests/test_openclaw_reply_delivery_tracker.py`

**Interfaces:**
- Consumes: OpenClaw `2026.7.1-2` `monitor.account-*.js`, `plugins/hook-runner-global.js`, and `plugin-sdk/hook-runtime.js`.
- Produces: idempotent `patch_openclaw_feishu_api_receipt(openclaw_root: Path) -> dict[str, object]` and direct API acceptance receipts containing `content`, `success`, `messageId`, `sessionKey`, and Feishu conversation context.

- [ ] **Step 1: Add failing runtime-patch tests**

  Add fixture-based tests that execute the patch against representative
  `monitor.account-*.js` source and assert observable behavior: normal send and
  streaming close invoke a fake hook runner once with the API message ID,
  while a missing message ID emits nothing. Add an idempotence run.

- [ ] **Step 2: Run RED tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_deployment_tools.py -k "feishu_message_sent_patch" -q
  ```

  Expected: failure because the patch module and managed `ExecStartPre` do not
  exist.

- [ ] **Step 3: Implement the minimal managed patch**

  Implement strict version detection, exact source anchors, atomic file
  replacement, and an idempotent injected helper equivalent to:

  ```javascript
  async function emitEimemoryFeishuMessageSent(params) {
    if (!params.messageId) return;
    const { getGlobalHookRunner } = await import("./plugins/hook-runner-global.js");
    const hooks = await import("./plugin-sdk/hook-runtime.js");
    const runner = getGlobalHookRunner();
    if (!runner?.hasHooks("message_sent")) return;
    const canonical = hooks.buildCanonicalSentMessageHookContext(params);
    await runner.runMessageSent(
      hooks.toPluginMessageSentEvent(canonical),
      hooks.toPluginMessageContext(canonical)
    );
  }
  ```

  Capture returned message IDs from direct/static sends and the initial
  streaming card state, emitting only when final visible content is settled.
  Add the patch as a second gateway `ExecStartPre`.

- [ ] **Step 4: Run GREEN tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_deployment_tools.py -k "feishu_message_sent_patch or openclaw_gateway_dropin" -q
  .\.venv\Scripts\python.exe -m pytest tests/test_openclaw_reply_delivery_tracker.py -q
  ```

  Expected: all selected tests pass.

---

### Task 2: Persist and Resume Release Closure Checkpoints

**Files:**
- Create: `eimemory/governance/release_closure_pending.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_release_closure.py`
- Test: `tests/test_openclaw_channel_acceptance.py`

**Interfaces:**
- Produces: `write_release_closure_pending(...)`, `read_release_closure_pending(...)`, `clear_release_closure_pending(...)`, and `reconcile_release_closure_pending(runtime, *, pending_path=None, delivery_state_path=None) -> dict[str, Any]`.
- Consumes: existing `record_openclaw_channel_acceptance`, persisted pre-channel stage reports, `current_release_identity`, and the post-channel portion of `run_release_closure`.

- [ ] **Step 1: Add failing checkpoint and resume tests**

  Cover these observable outcomes:

  ```python
  assert blocked["blocked_reason"] == "current_release_channel_receipt_not_found"
  assert pending["current_commit"] == CURRENT_COMMIT
  assert "version" not in pending
  assert resumed["ok"] is True
  assert runtime.weak_replay_calls == 1
  assert runtime.live_acceptance_calls == 1
  ```

  Also require no checkpoint for unrelated failures, stale commit rejection,
  malformed checkpoint rejection, and idempotent completion.

- [ ] **Step 2: Run RED tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_release_closure.py tests/test_openclaw_channel_acceptance.py -k "pending or reconcile or resume" -q
  ```

  Expected: failure because checkpoint persistence and reconciliation are
  absent.

- [ ] **Step 3: Implement atomic checkpoint storage**

  Store `release_closure_pending.v1` below
  `$EIMEMORY_ROOT/state/release-closure-pending.json`, using a same-directory
  temporary file, flush, fsync, atomic replace, strict schema validation, and a
  full-commit authority check. Persist only the stage reports needed to avoid
  rerunning weak replay and live acceptance.

- [ ] **Step 4: Extract post-channel continuation**

  Refactor the code after `record_openclaw_channel_acceptance` into one internal
  continuation used by both the initial closure and reconciler. The reconciler
  validates `same_release_authority(current_release_identity(...), pending)`
  before recording channel acceptance. It may run closure rehearsal/readiness,
  but cannot call autonomous learning commands or alter L5 maturity.

- [ ] **Step 5: Add Runtime and CLI entrypoints**

  Add:

  ```text
  eimemory learn release-closure-reconcile --json
  ```

  The command reads paths and identity from the checkpoint/default environment,
  prints a bounded report, returns zero for `no_pending` and
  `waiting_for_channel_acceptance`, and returns nonzero for malformed/stale
  state or failed resumed closure.

- [ ] **Step 6: Run GREEN tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_release_closure.py tests/test_openclaw_channel_acceptance.py -q
  ```

  Expected: all selected tests pass.

---

### Task 3: Install the Independent systemd Path Closure

**Files:**
- Create: `deploy/systemd/eimemory-release-closure.path`
- Create: `deploy/systemd/eimemory-release-closure.service`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `deploy/systemd/README.md`
- Modify: `eimemory/ops/timer_monitor.py`
- Test: `tests/test_deployment_tools.py`
- Test: `tests/test_timer_monitor.py`

**Interfaces:**
- Consumes: `/var/lib/eimemory/openclaw_reply_delivery_state.json` and
  `eimemory learn release-closure-reconcile`.
- Produces: an enabled path unit and guarded oneshot service with no timer and no
  watchdog dependency.

- [ ] **Step 1: Add failing installation and unit-behavior tests**

  Execute unit parsing/install helpers against temporary directories and assert:

  - `.path` watches the delivery ledger;
  - `.service` calls only `release-closure-reconcile`;
  - installer enables the path unit;
  - the service is included as a storage writer;
  - no release-closure timer or Feishu reply watchdog is installed.

- [ ] **Step 2: Run RED tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_deployment_tools.py tests/test_timer_monitor.py -k "release_closure or managed_runtime" -q
  ```

  Expected: failure because units and installation steps are absent.

- [ ] **Step 3: Implement systemd units and installer integration**

  Use:

  ```ini
  [Path]
  PathChanged=/var/lib/eimemory/openclaw_reply_delivery_state.json
  Unit=eimemory-release-closure.service
  ```

  The service uses `/opt/eimemory/current`, the standard EIMemory root/config
  environment, a runtime pycache path, and the CLI reconciler. Install and
  enable the path unit in candidate runtime metadata. Do not add a timer.

- [ ] **Step 4: Run GREEN tests**

  Run the same selected deployment/timer tests and require zero failures.

---

### Task 4: Focused Integration, Version, Commit, Deploy, and Notify

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: detected `CHANGELOG*.md` files when present

**Interfaces:**
- Produces: one patch release commit, pushed `origin/master`, deployed immutable
  release, verified production channel closure, and one Feishu notification.

- [ ] **Step 1: Run focused local verification**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_openclaw_reply_delivery_tracker.py tests/test_openclaw_channel_acceptance.py tests/test_release_closure.py tests/test_deployment_tools.py tests/test_timer_monitor.py -q
  .\.venv\Scripts\python.exe -m compileall -q eimemory deploy
  node --check integrations/openclaw/eimemory-bridge/index.js
  bash -n deploy/install_immutable_release.sh
  git diff --check
  ```

  Do not run the full suite.

- [ ] **Step 2: Advance patch metadata**

  Advance `1.9.108` to `1.9.109` in both version authorities and add a bounded
  changelog entry if changelogs exist. The version is metadata only and never
  enters channel or L5 authority checks.

- [ ] **Step 3: Commit and push**

  Create conventional implementation and release commits, verify a clean tree,
  and push `master` plus the matching release tag.

- [ ] **Step 4: Deploy from honxin**

  In `/dev-project/eimemory`, fast-forward `origin/master`, run:

  ```bash
  deploy/install_immutable_release.sh <full-40-character-commit>
  ```

  Verify the authoritative repo HEAD, `/opt/eimemory/current`, `/health`,
  gateway/RPC/loopback services, and managed path/service identity.

- [ ] **Step 5: Run real channel acceptance**

  Send one fresh post-deployment Feishu request, confirm exactly one reply,
  verify the ledger reaches `platform_accepted`, verify the path service resumes
  the current closure, and confirm L5 maturity/observation timestamps did not
  reset.

- [ ] **Step 6: Send Feishu completion notice**

  Send a concise notice containing version, abbreviated commit, deployment
  health, channel receipt result, release-closure result, L5 state, and the
  focused test count. Include no credentials or internal record identifiers.
