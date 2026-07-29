# OpenClaw Stability Cleanup Design

## Objective

Ship one patch release that removes the retired gateway restart mechanism from
the active source and runtime, bounds prompt-hook work within OpenClaw's outer
deadline, restores the remote eibrain monitor route, and makes release closure
consume Feishu acceptance receipts at a bounded cadence. After the new release
is verified, keep only that release under `/opt/eimemory/releases`.

## Observed root causes

### Retired restart mechanism was only partially removed

The user systemd timer and service no longer run, but the repository still
contains the Python implementation, its tests, the OpenClaw runtime patcher,
installer tombstones, CI references, and a gateway environment variable. The
installed OpenClaw package also contains the injected restart-recovery branch.
Removing only the unit therefore did not remove the behavior or its runtime
identity.

### Prompt work exceeds the caller's deadline

The `before_prompt_build` handler awaits the eibrain bridge and the memory hook
serially. Production config allows 12 seconds for the bridge and 25 seconds for
the hook while OpenClaw allows 25 seconds for the complete callback. The shared
child-process queue starts each child timeout only after dequeue, so queue delay
is unbounded from the caller's perspective.

### Channel acceptance is produced but closure stops consuming it

The current runtime has emitted real Feishu receipts with a platform message ID
and the current deployment commit. The closure path unit is triggered by every
reply-ledger write, including high-frequency progress writes. It reached the
systemd start limit before a qualifying receipt arrived, so later receipts were
never reconciled. This is a trigger-lifecycle defect, not evidence that the
current `message_sent` receipt patch is still losing every send.

### Monitor topology and payload contract drifted

The gateway points to loopback port 18080, where no monitor runs. The monitor is
reachable on the tailnet at `http://100.81.78.119:18080/status.json`. Its current
payload exposes `ok`, `status`, `checks`, and `body_runtime`; the eimemory
adapter only understands the older `system_health`, `visual_diagnostics`, and
`dialogue_diagnostics` shape.

## Design

### Source and runtime purge

Remove the restart implementation, quarantine patcher, their tests, CI entries,
gateway environment, installer references, and obsolete plan content from the
current tree. A zero-reference scan is an operational release gate, not a
committed source-text test.

The already-patched global OpenClaw package is restored by reinstalling the
exact published version `2026.7.1-2`, whose registry integrity is available.
The supported Feishu `message_sent` patch is then reapplied by the new gateway
unit. The Feishu reply-delivery implementation remains separate and is not
reactivated as a resend watchdog.

### One absolute prompt deadline

`before_prompt_build` creates one absolute deadline, defaulting to 22 seconds,
and launches bridge and memory-hook calls concurrently. `runCommand` carries
that absolute deadline through queue wait and child execution. A queued command
expires before it starts if the budget is exhausted; a running child receives
only the remaining budget. Fail-open transport handling remains unchanged, so
the prompt proceeds without the unavailable context rather than timing out the
whole OpenClaw callback.

### Bounded closure reconciliation

Replace the ledger-driven path unit with a timer that reconciles at most once
per 30-second interval, with a small randomized delay. The service keeps its
pending-checkpoint condition and single-process lock. Installation disables and
removes the old path unit, resets prior start-limit state, and enables the timer.
This preserves eventual consumption while making the trigger rate independent
of reply progress volume.

### Remote monitor compatibility

The managed gateway environment uses
`http://100.81.78.119:18080/status.json`. The numeric tailnet address is used
because MagicDNS resolution is not enabled on honxin. The transport accepts both the legacy status
shape and the current honjia supervisor shape. `health.status` maps `ok=true` to
healthy and a non-OK/degraded status to degraded while retaining legacy visual
and dialogue fields when present. Vision remains explicitly unavailable when
the current payload contains no visual observation.

## Deployment and destructive cleanup

1. Fast-forward the reviewed branch to `master`, push, and install the full
   commit as an immutable release.
2. Reinstall the exact OpenClaw package, restart the gateway, and verify that
   only the supported Feishu receipt patch remains.
3. Verify release identity, HTTP/RPC health, OpenClaw health, Feishu channel
   probe, a real platform acceptance receipt, closure timer behavior, and the
   monitor endpoint.
4. Resolve `/opt/eimemory/current` to an absolute directory and require that it
   equals the newly installed full commit.
5. Enumerate only immediate child directories of `/opt/eimemory/releases`.
   Delete every child except the resolved current directory.
6. Re-run identity and health probes after deletion and require exactly one
   release directory.

Git history and journal history are not rewritten. The current source tree,
active runtime, state paths, and release directory set contain no retired
restart implementation.

## Failure policy

No old release is deleted unless all pre-purge verification gates pass. Any
failed test, deployment mismatch, unavailable channel probe, missing real
receipt, or runtime marker stops the process while rollback releases still
exist. Post-purge verification failure is reported immediately and is not
masked as completion.
