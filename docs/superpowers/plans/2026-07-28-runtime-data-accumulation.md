# Runtime Data Accumulation Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate real learn and bridge evidence collection on honxin while keeping automatic promotion disabled and the L5 gate time-based.

**Architecture:** Apply host-only, persistent configuration through OpenClaw JSON policy and user-systemd drop-ins. Keep the deployed immutable release unchanged, use an atomic JSON update with a timestamped backup, and validate through runtime status plus one real prompt.

**Tech Stack:** OpenClaw, eimemory CLI, user systemd, JSON, SSH

## Global Constraints

- Do not change the deployed commit or application version.
- Do not run `eimemory-l5-observation-gate.service`; disable its timer because
  the service opens autonomous commit/deploy switches.
- Start the L5 loop directly and use a separate one-shot, read-only 48-hour
  effect-review timer.
- Do not reset, fabricate, or promote L5 evidence.
- Keep automatic candidate-to-canary promotion at zero.
- Never write SSH credentials to files, command lines, process arguments, or logs.

---

### Task 1: Capture the current production state

**Files:**
- Read: `/home/darrow/.openclaw/openclaw.json`
- Read: `/home/darrow/.config/systemd/user/`

**Interfaces:**
- Consumes: current honxin user service state and OpenClaw plugin policy
- Produces: a secret-safe baseline of commit identity, timer state, bridge flags, and promotion limits

- [ ] **Step 1: Verify deployment identity**

Run remotely:

```bash
git -C /dev-project/eimemory rev-parse HEAD
readlink -f /opt/eimemory/current
curl -fsS http://127.0.0.1:8091/health
```

Expected: repository HEAD, immutable release target, and health commit agree.

- [ ] **Step 2: Inspect the four timers and gateway runtime**

Run remotely:

```bash
systemctl --user is-enabled eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer eimemory-l5-observation-gate.timer
systemctl --user is-active eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer eimemory-l5-observation-gate.timer openclaw-gateway.service
systemctl --user show eimemory-l5-observation-gate.timer -p ActiveEnterTimestamp -p NextElapseUSecRealtime
systemctl --user show openclaw-gateway.service -p Environment
systemctl --user cat eimemory-nightly.service
```

Expected: a complete baseline without printing tokens or unrelated OpenClaw configuration.

### Task 2: Activate governed data collection

**Files:**
- Modify: `/home/darrow/.openclaw/openclaw.json`
- Create: `/home/darrow/.config/systemd/user/openclaw-gateway.service.d/40-eimemory-prompt-bridge.conf`
- Create: `/home/darrow/.config/systemd/user/eimemory-nightly.service.d/zz-disable-auto-promotion.conf`
- Create: `/home/darrow/.config/systemd/user/eimemory-nightly.service.d/zz-l5-start-now.conf`
- Create: `/home/darrow/.config/systemd/user/eimemory-l5-effect-review.service`
- Create: `/home/darrow/.config/systemd/user/eimemory-l5-effect-review.timer`
- Create: `/home/darrow/.config/systemd/user/eimemory-l5-effect-review.sh`

**Interfaces:**
- Consumes: the baseline and existing `eimemory-bridge` plugin entry
- Produces: enabled prompt injection/live bridge, scheduled learning, and a zero automatic-promotion budget

- [ ] **Step 1: Back up and atomically enable prompt policy**

Use a remote Python transaction that copies `openclaw.json` to a timestamped
mode-preserving backup, sets only
`plugins.entries.eimemory-bridge.hooks.allowPromptInjection=true`, writes a
same-directory temporary file, fsyncs it, preserves mode, and replaces the
original.

Expected: backup path recorded and unrelated JSON fields unchanged.

- [ ] **Step 2: Install persistent runtime drop-ins**

Write:

```ini
[Service]
Environment=EIMEMORY_ENABLE_PROMPT_INJECTION=1
Environment=EIMEMORY_ENABLE_PROMPT_BRIDGE=1
```

and:

```ini
[Service]
Environment=EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS=0
Environment=EIMEMORY_L5_MAX_PROMOTIONS=0
```

and:

```ini
[Service]
Environment=EIMEMORY_L5_LOOP_ENABLED=1
Environment=EIMEMORY_L5_LOOP_APPLY=1
Environment=EIMEMORY_L5_MAX_PROMOTIONS=0
```

Expected: all three files are owned by `darrow`, mode `0600`, and contain no secrets.

- [ ] **Step 3: Reload, enable timers, and restart the gateway**

Run remotely:

```bash
systemctl --user daemon-reload
systemctl --user enable --now eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer
systemctl --user disable --now eimemory-l5-observation-gate.timer
systemctl --user enable --now eimemory-l5-effect-review.timer
systemctl --user restart openclaw-gateway.service
```

Expected: the gateway, all three learn timers, and the effect-review timer are
active; the legacy activation gate is disabled and inactive.

### Task 3: Generate and verify real evidence

**Files:**
- Read: `/home/darrow/.openclaw/`
- Read: `/var/lib/eimemory/`

**Interfaces:**
- Consumes: the newly active runtime configuration
- Produces: one real prompt result plus service, timer, bridge, learning, and L5 evidence

- [ ] **Step 1: Query the bridge status tool**

Invoke `eimemory_bridge_status` through the running OpenClaw agent.

Expected: `hookCommandConfigured`, `bridgeCommandConfigured`,
`promptInjectionEnabled`, `promptInjectionEnvEnabled`,
`allowPromptInjection`, and `promptBridgeEnabled` are all true.

- [ ] **Step 2: Run one real, side-effect-free prompt**

Send a bounded prompt asking the agent to state whether eimemory context was
available and return a short answer.

Expected: successful OpenClaw agent completion with no external send or write.

- [ ] **Step 3: Run the initial observers after the prompt**

Run remotely:

```bash
systemctl --user start eimemory-learn-watch.service
systemctl --user start eimemory-learn-think.service
systemctl --user start eimemory-learn-dashboard.service
```

Expected: all three oneshots exit successfully and future timer runs remain scheduled.

- [ ] **Step 4: Start one production-bound L5 cycle**

Run with the production root/config environment:

```bash
/opt/eimemory/current/.venv/bin/eimemory learn l5 --apply --max-goals 1 --max-promotions 0 --json
```

Expected: the L5 world model, roadmap, goal graph, assessment, and reward are
persisted; the applied promotion count is zero.

- [ ] **Step 5: Perform final production checks**

Run remotely:

```bash
systemctl --user --failed --no-legend
systemctl --user is-enabled eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer eimemory-l5-effect-review.timer
systemctl --user is-active eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer eimemory-l5-effect-review.timer openclaw-gateway.service
systemctl --user is-enabled eimemory-l5-observation-gate.timer
systemctl --user is-active eimemory-l5-observation-gate.timer
systemctl --user list-timers --all eimemory-l5-effect-review.timer
curl -fsS http://127.0.0.1:8091/health
```

Expected: no failed units, the collection/effect timers are enabled and active,
the activation gate is disabled and inactive, the effect review is scheduled
48 hours later, the gateway is active, and deployment identity is unchanged.
