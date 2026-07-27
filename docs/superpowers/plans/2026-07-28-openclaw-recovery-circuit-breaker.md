# OpenClaw Recovery Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a pressure-triggered OpenClaw restart from resuming the failed workload, close Feishu delivery receipts at the real platform boundary, and make agent health checks strictly read-only.

**Architecture:** The eimemory watchdog persists a one-shot quarantine before it may restart the gateway. A version-gated OpenClaw compatibility patch consumes that quarantine in startup recovery, while a second version-gated patch emits canonical Feishu delivery hooks from the channel-specific dispatcher. A repository-managed health command reports local state without changing it.

**Tech Stack:** Python 3.11+, pytest, systemd user units, Bash, OpenClaw bundled JavaScript compatibility patches, Feishu plugin hooks.

## Global Constraints

- Limit changes to OpenClaw runtime compatibility, Feishu bridge, eimemory ops, deployment, tests, docs, and version metadata.
- Do not change recall, storage, L5, or autonomous evolution behavior.
- Keep the retired `openclaw-feishu-reply-watchdog` uninstalled, masked, and inactive.
- Never restart the gateway unless the recovery quarantine was persisted first.
- Never accept a Feishu delivery without a non-empty platform message ID.
- Never blindly resend historical delivery entries.
- Version must advance from `1.9.104` to `1.9.105`.
- Run focused repair tests only; do not run the full suite.
- GitHub, `/dev-project/eimemory`, `/opt/eimemory/current`, and runtime commit metadata must agree after deployment.
- Baseline note: `tests/test_deployment_tools.py::test_immutable_release_installer_commits_only_after_post_switch_gates` fails on `origin/master` because the script has five deploy-scope-user uses while the stale assertion expects four.

---

### Task 1: Persist Recovery Quarantine Before Restart

**Files:**
- Modify: `eimemory/ops/openclaw_watchdog.py`
- Modify: `tests/test_openclaw_watchdog.py`

**Interfaces:**
- Produces: `StuckSession(session_id: str, age_s: int)`.
- Produces: `CgroupPressure(memory_current_bytes, memory_high_bytes, memory_max_bytes, pids_current, pids_max)`.
- Produces: `parse_stuck_sessions(log_text: str) -> list[StuckSession]`.
- Produces: `collect_cgroup_pressure(control_group: str, *, cgroup_root: Path) -> CgroupPressure`.
- Produces: `write_recovery_quarantine(path: Path, *, trigger: str, now_ts: float, ttl_s: int, sessions: list[StuckSession]) -> dict`.
- Preserves: `parse_stuck_session_ages(log_text: str) -> list[int]` as a compatibility wrapper.

- [ ] **Step 1: Write failing parsing, inclusive-boundary, and cgroup tests**

```python
def test_parse_stuck_sessions_preserves_session_identity() -> None:
    logs = "[diagnostic] stuck session: sessionId=abc state=processing age=150s"
    assert parse_stuck_sessions(logs) == [StuckSession(session_id="abc", age_s=150)]


def test_hook_pressure_trips_at_inclusive_limit() -> None:
    assert has_hook_pressure(
        hook_count=5,
        hook_rss_kib=100,
        max_hook_processes=5,
        max_hook_rss_kib=1_638_400,
    )


def test_cgroup_pressure_trips_before_hard_limit() -> None:
    pressure = CgroupPressure(
        memory_current_bytes=2_800_000_000,
        memory_high_bytes=3_221_225_472,
        memory_max_bytes=4_294_967_296,
        pids_current=70,
        pids_max=96,
    )
    assert has_cgroup_pressure(
        pressure,
        max_memory_high_ratio=0.85,
        max_pids_ratio=0.70,
    )
```

- [ ] **Step 2: Run the new watchdog tests and verify red**

Run: `python -m pytest tests/test_openclaw_watchdog.py -q`

Expected: FAIL because the dataclasses and new functions do not exist and strict `>` boundaries still pass through.

- [ ] **Step 3: Implement structured parsing and resource collection**

```python
@dataclass(frozen=True)
class StuckSession:
    session_id: str
    age_s: int


@dataclass(frozen=True)
class CgroupPressure:
    memory_current_bytes: int = 0
    memory_high_bytes: int = 0
    memory_max_bytes: int = 0
    pids_current: int = 0
    pids_max: int = 0
```

Read cgroup scalar files defensively, treat `max` as zero/unbounded, and use inclusive comparisons for hooks, RSS, memory, and PIDs.

- [ ] **Step 4: Write failing quarantine ordering tests**

```python
def test_main_persists_quarantine_before_restart(tmp_path, monkeypatch) -> None:
    actions = []
    monkeypatch.setattr(watchdog_module, "write_recovery_quarantine",
                        lambda *args, **kwargs: actions.append("quarantine") or {})
    monkeypatch.setattr(watchdog_module.subprocess, "run",
                        lambda *args, **kwargs: actions.append("restart"))
    assert watchdog_module.main([...]) == 0
    assert actions == ["quarantine", "restart"]


def test_main_refuses_restart_when_quarantine_write_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        watchdog_module,
        "write_recovery_quarantine",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert watchdog_module.main([...]) == 2
```

- [ ] **Step 5: Implement atomic quarantine persistence**

Write `openclaw_recovery_quarantine.v1` with mode `targeted` when session IDs are known and `all_previous_lifecycle` otherwise. Use same-directory temporary creation, mode `0600`, `fsync`, and `os.replace`. Return exit code 2 without invoking systemd on persistence failure.

- [ ] **Step 6: Run focused watchdog tests**

Run: `python -m pytest tests/test_openclaw_watchdog.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add eimemory/ops/openclaw_watchdog.py tests/test_openclaw_watchdog.py
git commit -m "fix(ops): quarantine failed OpenClaw lifecycles"
```

---

### Task 2: Suppress Quarantined Startup Recovery

**Files:**
- Modify: `deploy/patch_openclaw_restart_recovery_scope.py`
- Create: `tests/test_openclaw_recovery_quarantine_patch.py`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`

**Interfaces:**
- Consumes: `/var/lib/eimemory/openclaw_recovery_quarantine.json`.
- Produces in patched runtime: `takeEimemoryRecoveryQuarantine()`.
- Produces in patched runtime: `shouldQuarantineRestartRecovery(quarantine, entry, sessionKey)`.
- Extends `recoverStore(params)` with `params.recoveryQuarantine`.

- [ ] **Step 1: Build a minimal real-shape recovery fixture and failing patch tests**

The fixture must contain the current `2026.7.1-2` anchors:

```javascript
async function recoverStore(params) {
  for (const [sessionKey, entry] of Object.entries(store)) {
    if (!entry || entry.status !== "running" || entry.abortedLastRun !== true) continue;
    if (await resumeMainSession({ entry, sessionKey })) result.recovered++;
  }
}
async function recoverRestartAbortedMainSessions(params = {}) {
  for (const storePath of await resolveRestartRecoveryStorePaths(params)) {
    const storeResult = await recoverStore({ storePath, resumedSessionKeys });
  }
}
```

Assert one patch application, idempotent second application, affected-version gating, exact-session suppression, all-lifecycle suppression, TTL rejection, malformed-state rejection, atomic consume-before-recovery, and source-anchor mismatch failure.

- [ ] **Step 2: Run the patch tests and verify red**

Run: `python -m pytest tests/test_openclaw_recovery_quarantine_patch.py -q`

Expected: FAIL because quarantine helpers and patch markers are absent.

- [ ] **Step 3: Extend the compatibility patch**

Insert version-gated JavaScript which:

```javascript
const EIMEMORY_RECOVERY_QUARANTINE_PATH =
  process.env.EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH
  || "/var/lib/eimemory/openclaw_recovery_quarantine.json";
```

The loader validates schema, timestamps, mode, and string arrays, then atomically renames the live marker before returning it. `recoverRestartAbortedMainSessions` loads once and passes the same object to every store. `recoverStore` sends the existing unresumable notice, calls `markSessionFailed`, increments `failed`, and never calls `resumeMainSession` for a quarantined entry.

- [ ] **Step 4: Add the quarantine path to the managed gateway environment**

```ini
Environment=EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH=/var/lib/eimemory/openclaw_recovery_quarantine.json
```

- [ ] **Step 5: Run patch and deployment tests**

Run: `python -m pytest tests/test_openclaw_recovery_quarantine_patch.py tests/test_deployment_tools.py::test_openclaw_restart_recovery_scope_patch_is_managed_and_idempotent -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add deploy/patch_openclaw_restart_recovery_scope.py deploy/systemd/openclaw-gateway-eimemory.conf tests/test_openclaw_recovery_quarantine_patch.py
git commit -m "fix(openclaw): suppress quarantined restart recovery"
```

---

### Task 3: Emit Real Feishu Platform Receipts

**Files:**
- Create: `deploy/patch_openclaw_feishu_delivery_hooks.py`
- Create: `tests/test_openclaw_feishu_delivery_patch.py`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`
- Modify: `integrations/openclaw/eimemory-bridge/index.js`
- Modify: `tests/test_openclaw_reply_delivery_tracker.py`

**Interfaces:**
- Produces in patched Feishu dispatcher: `emitEimemoryFeishuMessageSent(result, content, params)`.
- Consumes existing OpenClaw canonical hook helpers and global hook runner.
- Changes receipt correlation so platform success plus non-empty platform message ID is terminal even when rendered content differs from model final text.

- [ ] **Step 1: Write failing direct-dispatch patch tests**

Use a `2026.7.1-2` fixture with `createFeishuReplyDispatcher`, direct
`sendMessageFeishu`, `sendStructuredCardFeishu`, and chunk iteration. Assert that
the patch imports the current canonical hook mapper and hook runner, captures the
returned platform message ID, includes `params.sessionKey`, handles text and card
paths, and is idempotent.

- [ ] **Step 2: Run the Feishu patch tests and verify red**

Run: `python -m pytest tests/test_openclaw_feishu_delivery_patch.py -q`

Expected: FAIL because the patcher does not exist.

- [ ] **Step 3: Implement the version-gated Feishu dispatcher patch**

The inserted runtime helper must use:

```javascript
const canonical = buildCanonicalSentMessageHookContext({
  to: params.sendTarget,
  content,
  success: true,
  channelId: "feishu",
  accountId: params.accountId,
  conversationId: params.chatId,
  sessionKey: params.sessionKey,
  messageId
});
```

Call `hookRunner.runMessageSent(toPluginMessageSentEvent(canonical),
toPluginMessageContext(canonical))` through OpenClaw's bounded fire-and-forget
helper. Do not block or retry the platform send.

- [ ] **Step 4: Write a failing rendered-content mismatch regression**

```python
def test_platform_receipt_closes_even_when_rendered_text_differs() -> None:
    # agent_end records markdown final text; message_sent records a rendered chunk.
    # A real message ID must still close the latest entry exactly once.
    assert entry["status"] == "platform_accepted"
    assert entry["delivery_message_id"] == "om_platform"
```

- [ ] **Step 5: Relax only the receipt text equality gate**

Keep session and non-terminal-entry correlation. Require `event.success is true`
and a non-empty message ID. Use content matching to choose a candidate when
available, but do not reject a real receipt solely because rendering or chunking
changed text.

- [ ] **Step 6: Add the Feishu patch ExecStartPre**

Run the new patch after the existing OpenClaw scope/recovery patch:

```ini
ExecStartPre=/opt/eimemory/current/.venv/bin/python /opt/eimemory/current/deploy/patch_openclaw_feishu_delivery_hooks.py --openclaw-root %h/n/lib/node_modules/openclaw
```

- [ ] **Step 7: Run Feishu focused tests**

Run: `python -m pytest tests/test_openclaw_feishu_delivery_patch.py tests/test_openclaw_reply_delivery_tracker.py -q`

Expected: PASS with no resend-watchdog service introduced.

- [ ] **Step 8: Commit Task 3**

```bash
git add deploy/patch_openclaw_feishu_delivery_hooks.py deploy/systemd/openclaw-gateway-eimemory.conf integrations/openclaw/eimemory-bridge/index.js tests/test_openclaw_feishu_delivery_patch.py tests/test_openclaw_reply_delivery_tracker.py
git commit -m "fix(feishu): close replies at platform delivery boundary"
```

---

### Task 4: Replace Mutating Agent Health Check

**Files:**
- Create: `eimemory/ops/openclaw_health.py`
- Create: `deploy/openclaw/health-check.sh`
- Create: `tests/test_openclaw_health.py`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Produces: `build_health_report(*, run, urlopen, disk_usage) -> dict`.
- Produces CLI JSON with `ok`, `checks`, `degraded`, and bounded timing fields.
- Installs a workspace wrapper that only invokes the immutable release module.

- [ ] **Step 1: Write failing read-only health tests**

```python
def test_health_command_never_mutates_systemd() -> None:
    commands = []
    report = build_health_report(run=lambda command, **kwargs: commands.append(command) or completed())
    flattened = " ".join(" ".join(command) for command in commands)
    assert not any(verb in flattened for verb in (" restart ", " start ", " stop ", " enable "))


def test_workspace_wrapper_is_read_only() -> None:
    script = Path("deploy/openclaw/health-check.sh").read_text()
    assert "systemctl" not in script
    assert "openclaw_health" in script
```

- [ ] **Step 2: Run the health tests and verify red**

Run: `python -m pytest tests/test_openclaw_health.py -q`

Expected: FAIL because the module and wrapper do not exist.

- [ ] **Step 3: Implement bounded local probes**

Probe user-unit active state, OpenClaw loopback readiness, eimemory health, gateway
cgroup memory/PID limits, root disk usage, and recent gateway restart count. Every
subprocess and HTTP probe must have an explicit timeout. Return 0 only when required
local checks pass. Do not SSH to peer hosts and do not repair anything.

- [ ] **Step 4: Install the immutable wrapper into the workspace**

Add an installer function which creates
`$SERVICE_HOME/.openclaw/workspace/scripts` and installs
`deploy/openclaw/health-check.sh` as mode `0755`. Call it for both fresh deployment
and already-current metadata refresh.

- [ ] **Step 5: Update the stale baseline assertion**

Change the existing deploy-scope-user occurrence assertion from four to five after
confirming all five call sites pass the same explicit configured scope.

- [ ] **Step 6: Run health and deployment contract tests**

Run: `python -m pytest tests/test_openclaw_health.py tests/test_deployment_tools.py -q`

Expected: PASS except platform-specific skips.

- [ ] **Step 7: Commit Task 4**

```bash
git add eimemory/ops/openclaw_health.py deploy/openclaw/health-check.sh deploy/install_immutable_release.sh tests/test_openclaw_health.py tests/test_deployment_tools.py
git commit -m "fix(ops): make OpenClaw health checks read only"
```

---

### Task 5: Tighten Managed Watchdog and Advance Version

**Files:**
- Modify: `deploy/systemd/openclaw-stuck-watchdog.service`
- Modify: `deploy/systemd/openclaw-stuck-watchdog.timer`
- Modify: `tests/test_deployment_tools.py`
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`

**Interfaces:**
- Uses persistent state path `/var/lib/eimemory/openclaw_watchdog_state.json`.
- Uses quarantine path `/var/lib/eimemory/openclaw_recovery_quarantine.json`.
- Samples every 20 seconds.

- [ ] **Step 1: Write failing systemd threshold assertions**

Assert the service contains:

```text
--max-hook-processes 5
--max-hook-rss-mib 1600
--max-memory-high-ratio 0.85
--max-pids-ratio 0.70
--min-hook-pressure-samples 1
--quarantine-path /var/lib/eimemory/openclaw_recovery_quarantine.json
--state-path /var/lib/eimemory/openclaw_watchdog_state.json
```

Assert the timer contains `OnUnitActiveSec=20s`.

- [ ] **Step 2: Run the systemd contract test and verify red**

Run: `python -m pytest tests/test_deployment_tools.py::test_openclaw_watchdog_systemd_limits_stuck_and_hook_pressure -q`

Expected: FAIL on the old thresholds and one-minute timer.

- [ ] **Step 3: Update managed unit thresholds**

Keep `KillMode=control-group`, gateway cgroup limits, restart cooldown, and the
masked reply resend watchdog. Add the local ready URL only for stuck-log
suppression; resource pressure must continue to override a successful URL.

- [ ] **Step 4: Bump version to 1.9.105**

Set both:

```toml
version = "1.9.105"
```

```python
__version__ = "1.9.105"
```

- [ ] **Step 5: Run systemd and version tests**

Run: `python -m pytest tests/test_deployment_tools.py::test_openclaw_watchdog_systemd_limits_stuck_and_hook_pressure tests/test_version.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add deploy/systemd/openclaw-stuck-watchdog.service deploy/systemd/openclaw-stuck-watchdog.timer tests/test_deployment_tools.py pyproject.toml eimemory/version.py tests/test_version.py
git commit -m "release: prepare eimemory 1.9.105"
```

---

### Task 6: Focused Verification, Publish, Deploy, and Observe

**Files:**
- Verify all files changed by Tasks 1-5.
- No new implementation files unless a focused test exposes a defect.

**Interfaces:**
- Produces Git commit and runtime identity evidence.

- [ ] **Step 1: Run the focused repair suite**

Run:

```bash
python -m pytest \
  tests/test_openclaw_watchdog.py \
  tests/test_openclaw_recovery_quarantine_patch.py \
  tests/test_openclaw_feishu_delivery_patch.py \
  tests/test_openclaw_reply_delivery_tracker.py \
  tests/test_openclaw_health.py \
  tests/test_deployment_tools.py \
  tests/test_version.py -q
```

Expected: PASS except documented platform-specific skips.

- [ ] **Step 2: Run static verification**

Run:

```bash
python -m py_compile \
  eimemory/ops/openclaw_watchdog.py \
  eimemory/ops/openclaw_health.py \
  deploy/patch_openclaw_restart_recovery_scope.py \
  deploy/patch_openclaw_feishu_delivery_hooks.py
git diff --check
git status --short
```

Expected: no compilation error, no whitespace error, and only intentional files.

- [ ] **Step 3: Request code review and resolve findings**

Review the watchdog restart ordering, quarantine single-use behavior, JavaScript
patch anchors, Feishu no-duplicate invariant, and installer paths. Re-run only the
tests affected by any correction.

- [ ] **Step 4: Push the repair branch and fast-forward master**

Push the reviewed branch, then update GitHub `master` without rewriting history.
Confirm `git ls-remote origin refs/heads/master` equals the local final commit.

- [ ] **Step 5: Synchronize the honxin authoritative repository**

On honxin:

```bash
git -C /dev-project/eimemory fetch origin
git -C /dev-project/eimemory merge --ff-only origin/master
git -C /dev-project/eimemory status --short
```

Expected: clean repository at the GitHub commit.

- [ ] **Step 6: Deploy the exact commit**

Run the repository's immutable installer from `/dev-project/eimemory` with the full
commit SHA. Do not deploy from a workspace or staging checkout.

- [ ] **Step 7: Verify identity and live health**

Confirm:

- `/dev-project/eimemory` HEAD equals GitHub master;
- `/opt/eimemory/current` resolves to the same commit;
- `/health` reports version `1.9.105` and that commit;
- gateway environment and watchdog environment report that commit;
- OpenClaw health JSON, gateway status JSON, Feishu channel probe, loopback HTTP,
  eimemory RPC, Tailscale, Docker, disk, and stale loop lease are healthy;
- `openclaw-feishu-reply-watchdog` remains masked and inactive;
- the workspace health script contains no mutating systemctl operation.

- [ ] **Step 8: Perform a single Feishu acceptance turn**

Send one uniquely identifiable test request, verify exactly one reply, a non-empty
platform message ID, and a terminal `platform_accepted` ledger entry. Do not resend
on an uncertain lookup.

- [ ] **Step 9: Observe for at least 30 minutes**

At the start and end of the window, capture gateway start count, watchdog restart
count, hook count/RSS, cgroup memory/PIDs, event-loop delay, Feishu channel state,
and delivery ledger delta. Acceptance requires no new gateway restart, no resumed
quarantined session, no duplicate delivery, no stale lease, and no resource
pressure.

- [ ] **Step 10: Record final evidence**

Report the root cause, commits, focused test totals, deployment identity, Feishu
receipt, and observation-window counters. Distinguish current response from the
stronger closed-loop evidence.
