# Feishu API Receipt and Event-Driven Release Closure Design

**Date:** 2026-07-29  
**Scope:** OpenClaw Feishu automatic replies, EIMemory delivery evidence, and release-closure resumption

## Objective

Close the production channel evidence loop with the smallest truthful change:

1. promote an automatic Feishu reply only when the Feishu API returns a nonempty
   platform message ID; and
2. resume a release closure immediately after that receipt appears, without
   polling or rerunning already accepted release gates.

The design must not restore the deleted watchdog, bind L5 to a semantic version,
reset L5 progress, or couple channel delivery to autonomous learning timers.

## Chosen Architecture

Use the existing `message_sent` receipt contract and delivery ledger. Add a
narrow Feishu automatic-reply receipt adapter at the API success boundary so
ordinary messages, static cards, media, and streaming cards publish the same
canonical receipt event as generic outbound delivery.

Add an independent systemd path unit that watches the delivery ledger. A
guarded oneshot service resumes only a persisted current-commit
`release_closure_pending` checkpoint when a qualifying platform receipt exists.

## Invariants

1. `agent_end`, `dispatch complete`, and the absence of an exception are not
   platform acceptance.
2. `platform_accepted` requires `success=true` and a nonempty Feishu
   `messageId`.
3. The ledger schema and status meanings remain unchanged.
4. Release identity is commit plus deployment receipt, never the package
   version.
5. A stale-commit or pre-deployment inbound receipt cannot close the current
   release.
6. Channel reconciliation never starts learn/watch/think/candidate/canary,
   changes L5 observation time, resets L5 evidence, or performs an L5
   downgrade.
7. The deleted reply watchdog is not restored. `openclaw-loop-watch` remains an
   independent OpenClaw task-recovery loop and does not send channel replies.

## Component 1: Canonical Feishu API Receipt Adapter

The Feishu automatic-reply dispatcher already waits for the Feishu API. Its
send helpers return a platform result, but the dispatcher currently discards
that result and does not emit the plugin-facing `message_sent` hook.

After each successful external send, the adapter emits one canonical event:

```json
{
  "channelId": "feishu",
  "sessionKey": "agent:...:feishu:direct:...",
  "conversationId": "oc_...",
  "content": "canonical delivered content",
  "success": true,
  "messageId": "platform message id"
}
```

The adapter covers:

- normal post messages;
- static cards;
- media sends;
- the initial message created by a streaming card.

Streaming updates and close operations reuse the initial card message ID and do
not emit additional acceptance receipts for the same logical final reply.

If the API result is malformed, reports failure, or lacks a message ID, no
successful receipt is emitted. Existing bridge logic therefore leaves the
entry at `final_ready`.

The bridge continues to use `trackReplyMessageSent`. It must remain tolerant of
both event orders:

- `message_sent` before `agent_end`; and
- `agent_end` before `message_sent`.

Duplicate events with the same inbound, content, commit, and message ID are
idempotent.

## Component 2: Release Closure Pending Checkpoint

When the initial post-switch release closure has passed every stage before
channel acceptance and is blocked only by
`current_release_channel_receipt_not_found`, it writes an atomic checkpoint:

```json
{
  "schema_version": "release_closure_pending.v1",
  "status": "waiting_for_channel_acceptance",
  "current_commit": "<40 lowercase hex>",
  "prior_commit": "<40 lowercase hex>",
  "deployment_receipt_id": "<record id>",
  "scope": {
    "agent_id": "...",
    "workspace_id": "...",
    "user_id": "..."
  },
  "passed_gate_record_ids": {
    "deployment_receipt": "...",
    "production_recall_gate": "...",
    "production_recall_strict_state": "...",
    "replay_bootstrap": "...",
    "live_acceptance": ["..."]
  },
  "passed_gate_reports": {
    "replay_bootstrap": {},
    "live_acceptance": {},
    "bootstrap_pending": {}
  },
  "created_at": "<ISO-8601>"
}
```

The checkpoint contains no semantic version. It is written only for the one
expected channel-evidence gap; any other blocked stage remains a normal failed
closure and does not arm automatic resumption. The bounded stage reports retain
the exact inputs needed by the post-channel rehearsal so resumption never
reruns weak replay or live acceptance. Empty `bootstrap_pending` means the
production recall gate was already strict.

Checkpoint writes use a same-directory temporary file, fsync, and atomic
replace. A current-commit completion removes the checkpoint atomically.

## Component 3: Bounded Closure Resumption

`eimemory-release-closure.timer` activates every 30 seconds with a small
randomized delay.
`eimemory-release-closure.service` is a guarded oneshot.

The service:

1. acquires a nonblocking release-closure lock;
2. exits successfully when no pending checkpoint exists;
3. validates the checkpoint schema, current deployed commit, deployment
   receipt, scope, and pre-channel gate references;
4. scans for a direct Feishu `platform_accepted` entry whose inbound timestamp
   is after the deployment receipt and whose runtime commit is current;
5. exits successfully and leaves the checkpoint untouched when no candidate
   exists;
6. records channel acceptance and resumes the closure from the saved
   checkpoint;
7. clears the checkpoint only after the resumed closure returns `ok=true`.

The service does not invoke the broad pre-channel acceptance suite again. It
continues from channel acceptance using persisted gate evidence. Later
release-lineage and readiness evaluation may read current L5 evidence, but they
cannot start learning jobs or mutate monotonic L5 progress.

The timer is independent of learn timers and OpenClaw loop timers. Its bounded
interval prevents reply-ledger write volume from exhausting the service start
limit. Bounded systemd restart-on-failure handles transient execution errors;
persistent errors remain visible in the service status and journal.

## Failure Semantics

- **Missing message ID:** keep `final_ready`; do not trigger closure.
- **Content mismatch:** keep the entry nonterminal and persist a bounded
  diagnostic reason.
- **Duplicate receipt:** preserve the first accepted receipt and do not run a
  second closure.
- **Old commit or pre-receipt inbound:** ignore it for the current release.
- **Malformed checkpoint:** fail closed and preserve it for diagnosis.
- **Release changed while pending:** mark the checkpoint stale; never apply it
  to the new release.
- **Non-channel resumed failure:** retain the checkpoint and surface the exact
  blocked stage; do not claim closure.
- **Concurrent path events:** one lock owner performs reconciliation; other
  invocations exit without mutation.

## Deployment Integration

The immutable installer installs and enables the path unit and oneshot service.
It creates the pending checkpoint only after the current deployment receipt
exists and the initial release closure reports the specific missing-channel
condition.

The units run as the existing EIMemory service identity and use the same
governance environment files. Runtime paths point through
`/opt/eimemory/current`; the checkpoint itself binds execution to a full commit
and deployment receipt.

No reply watchdog unit, watchdog script, watchdog identity check, or watchdog
timer is added.

## Verification

Focused verification is sufficient; the full project suite is intentionally
excluded.

1. JavaScript delivery-tracker tests:
   - successful automatic API result with message ID reaches
     `platform_accepted`;
   - missing message ID remains `final_ready`;
   - both hook orderings converge;
   - duplicate receipt is idempotent;
   - streaming updates produce one logical receipt.
2. Python channel-acceptance and release-closure tests:
   - only post-deployment, current-commit, direct Feishu receipts qualify;
   - missing-channel closure writes a checkpoint;
   - unrelated failures do not arm resumption;
   - resume validates saved evidence and does not rerun pre-channel gates;
   - stale commits fail closed.
3. Deployment/systemd tests:
   - path and service are installed and enabled;
   - no timer is introduced;
   - no watchdog dependency is introduced;
   - service locking and idempotence work.
4. Static checks:
   - Python compile;
   - JavaScript syntax;
   - shell syntax;
   - `git diff --check`.
5. Production:
   - repository, deployed symlink, health commit, and runtime commit agree;
   - a real post-deployment Feishu inbound yields one platform message ID;
   - the ledger reaches `platform_accepted`;
   - the path service closes or truthfully records `data_accumulating`;
   - L5 progress and observation timestamps are not reset;
   - the user receives exactly one final reply.

## Non-Goals

- Recipient display/read confirmation beyond Feishu API acceptance.
- Replacing the reply ledger.
- Restoring polling, the deleted reply watchdog, or resend behavior.
- Advancing or downgrading L5.
- Binding closure eligibility to a package version.
- Running the full test suite for this focused change.
