# OpenClaw Recovery Circuit Breaker Design

## Context

Honxin entered a deterministic restart loop on 2026-07-28:

1. A Feishu main-session turn stalled while running collaborative child work.
2. `openclaw-hooks` fan-out pushed the gateway cgroup to its memory and PID limits.
3. The eimemory-managed watchdog restarted the gateway.
4. OpenClaw startup recovery marked the interrupted main session recoverable and
   submitted the same turn again.
5. The recovered turn recreated the pressure and the watchdog restarted it again.

The loop was amplified by three integration gaps:

- hook pressure used strict `>` thresholds and two one-minute samples, so exactly
  eight hooks and nearly 3 GiB of hook RSS did not trip the configured count gate;
- the workspace health script changed systemd state during an agent-requested
  health check;
- Feishu's channel-specific reply dispatcher sends directly and does not emit the
  generic outbound `message_sent` hook used by the eimemory delivery ledger.

The eimemory RPC, recall, L5, and autonomous evolution services were healthy during
the incident. This design does not change those subsystems.

## Goals

- A watchdog restart caused by a stuck session or gateway resource pressure must
  never resume work from the failed gateway lifecycle.
- Normal operator and upgrade restarts must retain OpenClaw's startup recovery.
- Resource pressure must be detected before the gateway reaches cgroup OOM or PID
  exhaustion.
- Agent-requested health checks must be read-only and bounded.
- A Feishu reply is terminal only after a real platform message ID is observed.
- Receipt repair must never create a second outbound delivery path.
- The source version, GitHub commit, honxin repository commit, immutable deployment,
  and runtime health identity must agree.

## Non-Goals

- Changing eimemory recall, storage, L5, or autonomous evolution behavior.
- Disabling OpenClaw restart recovery globally.
- Re-enabling the retired Feishu reply resend watchdog.
- Blindly resending historical `pending` or `final_ready` entries.
- Claiming that finite safeguards can prevent every future failure mode.

## Design

### 1. One-Shot Recovery Quarantine

The watchdog writes an atomic quarantine document before restarting the gateway.
The document contains a schema version, trigger, creation and expiry timestamps,
the previous gateway control-group identity, observed stuck session keys, and a
single-use state.

The OpenClaw restart-recovery compatibility patch reads the document during startup.
When the document is current and unconsumed, recovery from the previous lifecycle is
suppressed:

- matching stuck sessions are marked failed instead of being submitted to
  `resumeMainSession`;
- for a resource-pressure restart without a reliably parsed session key, all
  startup-orphaned main sessions from the interrupted lifecycle are quarantined;
- pending recovery markers are cleared;
- the quarantine is consumed atomically before any normal recovery may continue;
- the user receives at most one idempotent interruption notice through OpenClaw's
  existing failed-recovery delivery path.

Expired, malformed, already-consumed, manual, and upgrade restarts do not suppress
normal startup recovery. The runtime patch remains version-gated and fails closed
when its exact source anchors do not match.

### 2. Early Resource Circuit Breaker

The watchdog collects:

- stuck-session keys and ages;
- aged `openclaw-hooks` process count and aggregate RSS;
- gateway cgroup `memory.current`, `memory.high`, `memory.max`;
- `pids.current` and `pids.max`;
- cgroup memory-event counters.

Threshold comparisons are inclusive. A restart is allowed when any critical
resource limit is crossed, even if an HTTP endpoint still responds. HTTP health
continues to suppress a stuck-log-only restart, but it cannot mask resource
exhaustion.

The managed timer samples frequently enough to act before a second one-minute
interval, and the service has bounded subprocess and HTTP timeouts. Before the
restart, the watchdog persists quarantine and its own state. A cooldown still
prevents restart storms, while quarantine removes the failed workload that made the
old cooldown ineffective.

### 3. Read-Only Health Contract

The repository manages a bounded health-check script installed into the OpenClaw
workspace. Its default and only agent-facing mode:

- reads service state, health endpoints, cgroup counters, disk usage, and selected
  recent error counts;
- uses explicit timeouts;
- performs no `start`, `stop`, `restart`, `enable`, configuration edit, or remote
  repair;
- emits a compact machine-readable summary and exits non-zero when degraded.

Repairs remain explicit operator actions outside the health script. The old
self-healing behavior is removed so a health request cannot restart the gateway it
is diagnosing.

### 4. Feishu Platform Receipt Closure

OpenClaw's Feishu-specific dispatcher is patched at the actual send boundary to
emit the canonical `message_sent` plugin hook with:

- the direct Feishu session key;
- the actual platform message ID returned by Feishu;
- the delivered chunk content;
- success only after the platform API accepts the message.

The eimemory bridge correlates by session and the latest non-terminal inbound entry.
A successful platform ID closes the entry without requiring exact equality between
the model's final text and the rendered, chunked, quoted, or card-formatted text.
Content remains a preference for disambiguating multiple candidates, not a
condition for accepting a platform receipt.

Historical entries are reconciled passively against existing Feishu replies. The
retired resend watchdog stays disabled. Missing or uncertain receipts remain
visible for operator review and are never silently converted to success.

## Failure Handling

- Quarantine writes use same-directory atomic replace and restrictive permissions.
- A quarantine persistence failure aborts the watchdog restart; restarting without
  the circuit breaker would recreate the incident.
- Runtime patch anchor or version mismatch aborts gateway startup rather than
  running an unverified compatibility mutation.
- A receipt hook failure does not block Feishu delivery, but leaves the ledger
  non-terminal and emits a bounded diagnostic.
- Health checks fail open with respect to service state: they report but never
  repair.

## Verification

Focused tests cover:

- stuck-session parsing and exact-key quarantine;
- one-shot, TTL, malformed-state, and resource-pressure global quarantine;
- inclusive hook, memory, and PID thresholds;
- quarantine persistence before restart and restart refusal on persistence failure;
- normal manual restart recovery when no quarantine exists;
- the real OpenClaw recovery and Feishu bundle shapes for the affected version;
- direct, formatted, and chunked Feishu platform receipts;
- successful message IDs closing receipts without exact text equality;
- no install or enable path for the retired reply resend watchdog;
- the read-only health script containing no mutating systemctl verbs.

After deployment, acceptance requires:

- GitHub, `/dev-project/eimemory`, `/opt/eimemory/current`, runtime commit, and
  version `1.9.105` to agree;
- fresh HTTP, gateway RPC, Feishu channel, and eimemory health probes;
- one Feishu test turn accepted exactly once with a platform message ID;
- no stale OpenClaw loop lease;
- no gateway restart, quarantine replay, hook pressure, or delivery duplicate during
  a minimum 30-minute observation window.

## Rollback

The immutable deployment retains the previous release. Rollback restores the prior
release symlink and managed systemd files together. Because quarantine is
single-use and versioned, rollback code ignores it. The previous OpenClaw package
backup produced by the compatibility patch remains available for package-level
rollback.
