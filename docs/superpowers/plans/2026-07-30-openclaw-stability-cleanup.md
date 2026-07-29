# OpenClaw Stability Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release and deploy a clean eimemory version that removes the retired gateway restart mechanism, bounds prompt hooks, restores eibrain monitor access, debounces closure reconciliation, and leaves only the verified new immutable release.

**Architecture:** Remove the retired restart/quarantine path completely, run prompt context providers concurrently under one absolute deadline, replace ledger-edge closure activation with a bounded timer, and translate both legacy and current honjia monitor payloads. Destructive release pruning occurs only after deployment, channel, identity, and closure verification.

**Tech Stack:** Python 3.14, Node.js CommonJS OpenClaw plugin, pytest, systemd user units, Bash immutable installer, npm OpenClaw package.

## Global Constraints

- Authoritative repository: `/dev-project/eimemory`.
- Development branch: `fix/stuck-watchdog-closure`, based on `origin/master`.
- Production release is installed only from a full 40-character commit.
- Target version: `1.9.116`.
- Preserve the Feishu receipt hook and do not reactivate timer-based reply resend.
- Do not rewrite Git history or journald history.
- Do not delete any old immutable release before every pre-purge gate passes.
- After purge, `/opt/eimemory/releases` contains exactly the new current release.

---

### Task 1: Commit approved design and plan

**Files:**
- Create: `docs/superpowers/specs/2026-07-30-openclaw-stability-cleanup-design.md`
- Create: `docs/superpowers/plans/2026-07-30-openclaw-stability-cleanup.md`

**Interfaces:**
- Consumes: approved user scope and production evidence.
- Produces: the implementation contract used by every later task.

- [ ] **Step 1: Validate the documents**

Run:

```bash
python - <<'PY'
from pathlib import Path
for name in (
    "docs/superpowers/specs/2026-07-30-openclaw-stability-cleanup-design.md",
    "docs/superpowers/plans/2026-07-30-openclaw-stability-cleanup.md",
):
    text = Path(name).read_text(encoding="utf-8")
    assert len(text) > 1000
    assert "/opt/eimemory/releases" in text
PY
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-30-openclaw-stability-cleanup-design.md \
        docs/superpowers/plans/2026-07-30-openclaw-stability-cleanup.md
git commit -m "docs: design OpenClaw stability cleanup"
```

### Task 2: Remove the retired restart and quarantine implementation

**Files:**
- Delete: `eimemory/ops/openclaw_watchdog.py`
- Delete: `deploy/patch_openclaw_restart_recovery_scope.py`
- Delete: `tests/test_openclaw_watchdog.py`
- Delete: `tests/test_openclaw_recovery_quarantine_patch.py`
- Delete: `docs/superpowers/plans/2026-07-28-openclaw-recovery-circuit-breaker.md`
- Modify: `.github/workflows/linux-deployment-contracts.yml`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`
- Modify: `tests/test_deployment_tools.py`
- Modify: obsolete design and plan documents that name removed artifacts

**Interfaces:**
- Consumes: the current managed gateway and installer.
- Produces: a source tree with no restart/quarantine implementation or deployment hook.

- [ ] **Step 1: Run the external zero-reference gate and observe RED**

```bash
python - <<'PY'
from pathlib import Path
needles = (
    "openclaw-stuck-watchdog",
    "openclaw_watchdog",
    "openclaw_recovery_quarantine",
    "EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH",
)
hits = []
for path in Path(".").rglob("*"):
    if path.is_file() and ".git" not in path.parts:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(item.lower() in text.lower() for item in needles):
            hits.append(str(path))
assert not hits, hits
PY
```

Expected: FAIL and list the implementation, tests, installer, CI, and documents.

- [ ] **Step 2: Delete implementation and test artifacts**

Delete the five files listed above. Remove the quarantine `Environment=` line
and restart patcher `ExecStartPre=` line from the gateway drop-in. Remove the
stale unit cleanup block from the installer and the retired test from CI.
Update `tests/test_deployment_tools.py` so it continues to verify the supported
loop monitor, Feishu receipt patch, and release-closure units without any
retired restart references.

- [ ] **Step 3: Remove remaining current-tree references**

Rewrite obsolete plan/spec wording to describe a generic removed restart
component or delete obsolete documents when the entire document implements the
removed design.

- [ ] **Step 4: Run the zero-reference gate and focused tests**

Run the Step 1 gate again; expected PASS. Then run:

```bash
python -m pytest -q tests/test_deployment_tools.py
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove retired OpenClaw restart path"
```

### Task 3: Enforce one absolute prompt deadline

**Files:**
- Modify: `integrations/openclaw/eimemory-bridge/index.js`
- Modify: `tests/test_platform.py`

**Interfaces:**
- Consumes: `scheduleCommand(start)`, `runCommand(...)`, `safeInvokeHook(...)`, and `safeInvokeBridge(...)`.
- Produces: `deadlineAtMs` propagation through queue wait and child execution, plus concurrent prompt providers.

- [ ] **Step 1: Add a failing concurrent-provider test**

Create
`test_openclaw_before_prompt_build_starts_bridge_and_hook_under_one_deadline`.
Use two real Node child scripts that append their start timestamps and respond
after 120 ms. Enable prompt injection and bridge prompt. Assert that both
process starts are less than 80 ms apart and that the result includes both
bridge and memory context.

Run:

```bash
python -m pytest -q tests/test_platform.py::test_openclaw_before_prompt_build_starts_bridge_and_hook_under_one_deadline
```

Expected: FAIL because the hook starts only after the bridge exits.

- [ ] **Step 2: Add a failing queue-budget test**

Create `test_openclaw_command_deadline_includes_queue_wait`. Saturate a
single-concurrency command slot with a 200 ms terminal hook, invoke a second
hook with a 60 ms deadline, and assert the second child never starts after the
slot becomes free.

Run the new test; expected: FAIL because the existing child timeout starts only
after dequeue.

- [ ] **Step 3: Implement absolute deadlines**

Add `DEFAULT_BEFORE_PROMPT_BUDGET_MS = 22000`. Let `runCommand` accept
`deadlineAtMs`; compute the remaining time before spawn and use the minimum of
the configured child timeout and remaining budget. Let `scheduleCommand`
expire and remove queued work at its absolute deadline with code
`EIMEMORY_QUEUE_TIMEOUT`. Clear queue timers when work starts.

Add `configuredBeforePromptBudget(api)` backed by
`EIMEMORY_BEFORE_PROMPT_BUDGET_MS`. In `before_prompt_build`, create one
deadline and run the eligible bridge plus memory hook with `Promise.all`, both
receiving the same deadline.

- [ ] **Step 4: Verify GREEN and surrounding behavior**

```bash
python -m pytest -q \
  tests/test_platform.py::test_openclaw_before_prompt_build_starts_bridge_and_hook_under_one_deadline \
  tests/test_platform.py::test_openclaw_command_deadline_includes_queue_wait \
  tests/test_platform.py::test_openclaw_js_bridge_bounds_concurrent_hook_processes \
  tests/test_platform.py::test_openclaw_js_bridge_coalesces_identical_prompt_recall
```

- [ ] **Step 5: Commit**

```bash
git add integrations/openclaw/eimemory-bridge/index.js tests/test_platform.py
git commit -m "fix: bound OpenClaw prompt hook deadline"
```

### Task 4: Restore the honjia monitor route and schema

**Files:**
- Modify: `eimemory/ei_bridge/eibrain_monitor.py`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`
- Modify: `tests/test_ei_bridge_integration.py`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Consumes: honjia `status.json` with either the legacy diagnostic shape or the current supervisor shape.
- Produces: stable `health.status` and explicit vision availability.

- [ ] **Step 1: Add a failing current-schema test**

Add a fixture with:

```python
{
    "ok": True,
    "status": "ready",
    "checks": {"body_runtime_snapshot": "ok"},
    "body_runtime": {},
}
```

Assert that `health.status` returns `system_health == "healthy"` and vision
remains unavailable. Add a degraded fixture with `ok=False`.

Run:

```bash
python -m pytest -q tests/test_ei_bridge_integration.py
```

Expected: FAIL because the old adapter reports `unknown`.

- [ ] **Step 2: Implement dual-schema normalization**

Normalize `system_health` from the legacy field when present; otherwise map
`ok is True` to `healthy`, `ok is False` to `degraded`, and use the nonempty
`status` value as a fallback. Preserve existing legacy visual/dialogue mapping.
Set the default and managed production URL to
`http://honjia:18080/status.json`.

- [ ] **Step 3: Verify GREEN**

```bash
python -m pytest -q tests/test_ei_bridge_integration.py tests/test_deployment_tools.py
```

- [ ] **Step 4: Commit**

```bash
git add eimemory/ei_bridge/eibrain_monitor.py \
        deploy/systemd/openclaw-gateway-eimemory.conf \
        tests/test_ei_bridge_integration.py tests/test_deployment_tools.py
git commit -m "fix: route eibrain monitor over tailnet"
```

### Task 5: Debounce release-closure reconciliation

**Files:**
- Create: `deploy/systemd/eimemory-release-closure.timer`
- Delete: `deploy/systemd/eimemory-release-closure.path`
- Modify: `deploy/systemd/eimemory-release-closure.service`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `deploy/systemd/README.md`
- Modify: `tests/test_deployment_tools.py`
- Modify: `tests/test_release_closure.py` only if behavior coverage needs adjustment

**Interfaces:**
- Consumes: the canonical pending checkpoint and reply ledger.
- Produces: at most one reconcile activation per 30-second interval.

- [ ] **Step 1: Add a failing deployment behavior test**

Replace the existing path-unit test with
`test_release_closure_timer_bounds_reconcile_frequency`. Parse the timer unit
and assert `OnUnitActiveSec=30s`, `RandomizedDelaySec=5s`, the reconcile
service unit, and absence of a ledger path unit from installed artifacts.
Assert the installer disables/removes the obsolete path unit, resets failed
state, and enables/starts the timer.

Run:

```bash
python -m pytest -q tests/test_deployment_tools.py::test_release_closure_timer_bounds_reconcile_frequency
```

Expected: FAIL because the timer does not exist.

- [ ] **Step 2: Implement timer activation**

Create a user timer using `OnBootSec=30s`, `OnUnitActiveSec=30s`,
`AccuracySec=2s`, `RandomizedDelaySec=5s`, and
`Unit=eimemory-release-closure.service`. Keep
`ConditionPathExists` and the file lock in the service. Change the installer
to migrate from the path unit, reset failed state, and enable/start the timer.

- [ ] **Step 3: Verify GREEN and closure logic**

```bash
python -m pytest -q \
  tests/test_deployment_tools.py::test_release_closure_timer_bounds_reconcile_frequency \
  tests/test_release_closure.py
```

- [ ] **Step 4: Commit**

```bash
git add -A deploy/systemd deploy/install_immutable_release.sh \
  tests/test_deployment_tools.py tests/test_release_closure.py
git commit -m "fix: debounce release closure reconciliation"
```

### Task 6: Prepare patch release 1.9.116

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: completed feature commits.
- Produces: one version identity shared by source, package, and deployment.

- [ ] **Step 1: Update version and changelog**

Set both version files to `1.9.116`. Add a dated changelog entry describing the
restart-path purge, prompt deadline, monitor route, and closure timer.

- [ ] **Step 2: Run focused and full verification**

```bash
python -m pytest -q \
  tests/test_platform.py \
  tests/test_openclaw_reply_delivery_tracker.py \
  tests/test_ei_bridge_integration.py \
  tests/test_release_closure.py \
  tests/test_deployment_tools.py
python -m pytest -q
python -m compileall -q eimemory deploy
git diff --check
```

- [ ] **Step 3: Commit and tag**

```bash
git add pyproject.toml eimemory/version.py CHANGELOG.md
git commit -m "chore: release v1.9.116"
git tag v1.9.116
```

### Task 7: Integrate, push, and deploy

**Files:**
- No new source files.

**Interfaces:**
- Consumes: verified branch tip and tag.
- Produces: `origin/master` and production current identity at the same full commit.

- [ ] **Step 1: Fast-forward master and push**

From `/dev-project/eimemory`, fetch, verify `master == origin/master`, merge the
feature branch with `--ff-only`, run the focused suite once on merged master,
then push `master` and `v1.9.116`.

- [ ] **Step 2: Install the immutable release**

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
```

- [ ] **Step 3: Restore the exact OpenClaw package**

Stop the gateway, reinstall
`openclaw@2026.7.1-2` with `/home/darrow/n/bin/npm`, restart the gateway so the
supported Feishu receipt patch is applied, and verify there is no removed
restart marker in the installed package.

- [ ] **Step 4: Verify before purge**

Require all of:

```text
/health version == 1.9.116
/health commit == deployed full commit
/opt/eimemory/current == /opt/eimemory/releases/<full commit>
RPC ready and migration complete
OpenClaw HTTP/RPC/channel probes succeed
gateway runtime commit == deployed full commit
honjia monitor returns HTTP 200 and health.status is normalized
closure timer is active and service is not start-limit-hit
a new direct Feishu reply has platform_accepted with the deployed commit
current-tree and active-runtime removed-marker scans are empty
```

### Task 8: Purge old immutable releases and verify again

**Files:**
- Destructive production operation limited to immediate child directories of `/opt/eimemory/releases`.

**Interfaces:**
- Consumes: successful Task 7 verification.
- Produces: exactly one immutable release, the verified current commit.

- [ ] **Step 1: Resolve and validate targets**

Resolve `/opt/eimemory/current`, require it to equal
`/opt/eimemory/releases/<deployed full commit>`, and enumerate all immediate
release child directories. Abort if the current target is missing or outside
the releases directory.

- [ ] **Step 2: Delete every noncurrent immediate child**

Use one remote Python cleanup program that resolves each child, requires the
parent to equal `/opt/eimemory/releases`, skips the exact current path, and
removes only validated noncurrent directories.

- [ ] **Step 3: Remove active state residue**

Remove matching retired systemd paths, quarantine state files, and targeted
test artifacts. Reload the user systemd manager.

- [ ] **Step 4: Run final verification**

Require exactly one release directory, current identity agreement, healthy
HTTP/RPC/OpenClaw/Feishu probes, active closure timer, reachable monitor, and
zero removed runtime/source markers.
