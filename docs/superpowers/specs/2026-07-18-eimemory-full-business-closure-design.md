# EIMemory Full Business Closure Design

**Date:** 2026-07-18
**Target:** one release candidate after `1.9.69`; the version is advanced only after every gate in this document passes
**Scope:** RPC security, OpenClaw compatibility, external-knowledge trust, real-task/L5 evidence, prompt safety, Feishu delivery, storage recovery, source-registry concurrency, replay sequencing, intake transport, and immutable deployment

## Context

The deployed `1.9.69` process is healthy and matches repository commit
`2ef2febc353938013f03d765b566fa6acea50a42`, but service health is not business
closure. The audit established the following counterexamples:

- a non-loopback RPC listener starts without an authentication token;
- external content can assert `source_trust=1.0` and pass the capability gate;
- nonexistent record identifiers can produce a persisted `L5` assessment;
- operational probes are counted as current-deployment business tasks;
- Feishu items can remain status-only pending indefinitely and a failed attempt
  write can cause duplicate sends;
- JSONL recovery silently drops malformed lines and reports success;
- source-registry updates and replay manifest sequences race across processes;
- intake validates a hostname and then reconnects by hostname, allowing DNS
  rebinding;
- the immutable installer marks the switch committed before registry refresh,
  restarts, and health validation;
- prompt safety still identifies itself as a stub while downstream governance
  can nevertheless reach L5.

This design treats these findings as one release boundary. A partial green state
must not advance the version.

## Chosen Approach

Use a staged hardening release on the existing architecture. Add narrow,
testable contracts at each trust and durability boundary instead of replacing
the entire storage or governance architecture.

Two alternatives were rejected:

1. A P0/P1-only hotfix would leave delivery, storage recovery, and L5 evidence
   open and therefore would not meet the closure objective.
2. A new unified event-ledger architecture could remove more historical
   complexity, but it would turn a repair release into a high-risk migration.
   The present release instead makes the existing JSONL/SQLite design explicit
   and recoverable.

## Global Invariants

1. No caller-controlled field may elevate trust, privilege, evidence quality,
   or autonomy level.
2. A status is not evidence. Every closure decision must resolve persisted
   records and validate kind, status, scope, provenance, and release identity.
3. Operational probes and real business outcomes are separate evidence classes.
4. Every externally visible delivery reaches a durable terminal state or a
   durable escalation state; an uncertain send is never retried automatically.
5. Every recovery or deploy command fails closed on corruption, ambiguity, or
   identity mismatch.
6. The final release is one commit identity, one declared version, one package
   digest, and one production receipt.
7. Data accumulation is the only permitted post-release L5 gap. When the real
   task thresholds are not met, the stage is `data_accumulating`, never `L5`.

## 1. RPC Trust Boundary

### Server policy

`EIBrainRPCServer` classifies its bind address before accepting traffic.

- Loopback binds may expose `GET /health` without authentication.
- Every RPC method and every non-health endpoint requires a bearer token.
- A non-loopback bind refuses to start unless the token contains at least 32
  characters and 128 bits of effective entropy after normalization.
- An empty or weak token is a configuration error, not a request-time opt-out.
- Token comparisons remain constant-time and authentication failures return
  `401` without revealing token details.

### Deployment policy

The installer creates `/etc/eimemory/rpc.env` only when it is absent, using a
cryptographically random URL-safe token, mode `0640`, and the configured service
user/group. An existing weak or unreadable file aborts deployment. The systemd
unit requires the environment file rather than silently ignoring it. Health
checks remain loopback-local and do not need the secret.

## 1A. OpenClaw 2026.7.1 Compatibility Boundary

The bridge declares `openclaw.compat.pluginApi >=2026.7.1`, uses one canonical
native plugin root, and keeps the manifest hook list synchronized with every
runtime registration. Raw conversation hooks require host-side
`allowConversationAccess=true`; prompt injection remains independently gated
until L5 readiness authorizes it.

Deployment refreshes the plugin registry, restarts the Gateway, and requires
`openclaw plugins inspect eimemory-bridge --runtime --json` to succeed. A
`message_sent` event or message-tool receipt proves platform acceptance only,
not recipient display or read.

## 2. Server-Derived External-Knowledge Trust

Introduce a single trust resolver used by ingest, recall, answer evidence, skill
candidate creation, and capability validation.

The resolver consumes:

- normalized source URI;
- connector identity supplied by server code;
- an enabled source-registry entry whose source ID and normalized URI match;
- operator-controlled registry trust metadata;
- stored verification evidence produced by EIMemory.

The resolver never consumes caller `source_trust`, `trust`, `confidence`, or
`reliability` as authority. Those values are preserved under
`diagnostic_claimed_trust` for audit only.

Trust is capped as follows:

- unregistered external material: `0.50`;
- registered RSS/news/blog/manual material: at most `0.65`;
- registered paper or verified repository material: at most `0.85`;
- operator-verified official/API documentation with matching URI: at most
  `1.00`.

Only server-derived trust at or above `0.80` may enter capability generation.
Low-trust material can remain quarantined research evidence but is excluded from
answer context and autonomous promotion. Stored records carry
`trust_authority=eimemory.source_trust.v1`, registry ID, normalized URI, and a
policy digest so later consumers can recompute rather than blindly accept the
score.

## 3. Evidence Contract and L5 Semantics

### Evidence resolver

Add one governance evidence resolver that validates every referenced record.
Each reference must resolve in the requested scope and satisfy an expected kind,
allowed status, trusted internal source, and required metadata. Release-bound
evidence must also match the current commit, version, deployment receipt, and
release-session identifier.

`assess_l5_closed_loop` uses resolved records, not fields copied from a supplied
dictionary. Missing, wrong-kind, cross-scope, stale-release, quarantined, or
untrusted references are included in `missing_evidence` with a machine-readable
reason. A fake identifier therefore cannot increase the assessed level.

### Evidence classes

Existing live-acceptance checks become `operational_probe` evidence. They verify
that deployed mechanics work but never count toward business success.

A `verified_real_task` requires all of the following:

- a persisted OpenClaw task event and terminal outcome linked by event ID;
- a non-rehearsal external correlation ID such as a Feishu message ID;
- server-derived source attribution of `user_explicit` or `system_verified`;
- terminal success/failure evidence and an outcome trace;
- current deployment commit, version, receipt ID, and release session;
- a nonempty normalized task type.

The capability dashboard exposes separate metrics:

- `current_deployment_operational_probe_success_rate`;
- `current_deployment_verified_real_task_success_rate`;
- `current_deployment_verified_real_tasks`;
- `current_deployment_verified_real_task_types`.

Legacy live-task metric names may remain as compatibility aliases for one
release, but L5 never reads them.

### L5 gate

L5 requires:

- at least 10 current-release verified real tasks;
- at least 5 distinct task types;
- real-task success rate at least `0.80`;
- current-release replay pass rate at least `0.80` with no missing weak
  capability;
- validated world model, roadmap, goal graph, candidate, replay,
  promotion/block, reward transition, rollback/stop condition, and
  self-continuity evidence;
- a current-release deployment receipt and operational-probe pass;
- an operational prompt-safety assessment;
- no unresolved manifest collisions, trust violations, or release-identity
  mismatches.

If structural/process evidence is complete but real-task counts are below the
threshold, readiness reports `stage=data_accumulating`, lists the exact sample
and type deficits, and keeps `complete=false`. Stale L5 snapshots remain audit
history and cannot represent the current deployment.

## 4. Operational Prompt-Safety Gate

Replace the stub marker with an executable battery contract.

The deterministic scanner remains a cheap prefilter. The operational gate also
runs a configured executor against a versioned case set containing clean
controls, direct injection, indirect injection, role override, tool
exfiltration, and policy-bypass cases. A result is operational only when:

- every case was actually executed;
- output records contain executor identity, model, case-set digest, timestamps,
  and per-case verdicts;
- clean controls pass and adversarial cases remain within expected boundaries;
- the assessment is bound to the current release.

Executor absence, timeout, malformed output, or incomplete cases produces
`not_ready` and blocks L5. Tests use deterministic fake executors; production
closure uses the configured OpenClaw model path and stores the assessment.

## 5. Feishu Delivery State Machine

Use a durable state machine keyed by incoming message ID and response content
hash:

`pending -> status_notified -> final_ready -> sending -> platform_accepted`

Terminal alternatives are `failed`, `escalated`, and `delivery_uncertain`.

Before invoking the external send command, the watchdog atomically persists a
`sending` intent with delivery key, target, content hash, attempt number, and
timestamp. If that persistence fails, it does not send. After the command
returns, it atomically stores the tool receipt and terminal state. The receipt
proves platform acceptance only; state and field names must not imply recipient
display or read.

If the process crashes after the external send but before receipt persistence,
the next scan sees `sending` and does not resend. It attempts receipt
reconciliation; if proof cannot be recovered by the SLA, it transitions to
`delivery_uncertain` and escalates. This chooses at-most-once delivery over a
duplicate reply because the external CLI does not expose an idempotency key.

Status notices do not satisfy final delivery. Pending entries have explicit
deadlines: overdue work is resumed if a resumable task reference exists;
otherwise it becomes `escalated` with a durable reason. Attempt-journal write
errors make the watchdog invocation fail nonzero and appear in service health.

## 6. Durable Storage and Recovery

SQLite is the online authoritative state. JSONL files are durable export and
disaster-recovery streams managed by an SQLite outbox.

Each mutating SQLite transaction writes the business row and an outbox row in
the same transaction. The outbox row contains stream name, stable operation ID,
canonical JSON payload, payload digest, and export state. The synchronous flush
appends and fsyncs the JSONL entry, then marks the outbox row exported. Startup
and an explicit repair command retry unexported rows idempotently. A stable
operation ID in each JSONL envelope prevents duplicate exports.

Recovery scans JSONL in binary mode and records stream, line number, byte
offset, and parse/digest error. Default rebuild behavior is strict: any malformed
line, duplicate operation with a conflicting digest, missing high-water marker,
or stream/outbox count mismatch returns `ok=false` and does not replace the live
database. An explicit diagnostic mode may report all errors but may not install
the rebuilt database. Replacement builds a temporary database, validates it,
fsyncs it, and atomically swaps it into place.

Existing legacy JSONL entries without operation IDs remain readable and are
assigned deterministic legacy IDs from stream, offset, and payload digest.

## 7. Atomic Source Registry and Replay Sequences

### Source registry

Source-registry mutation uses an interprocess file lock. Under the lock it
reloads current content, applies one mutation, writes a same-directory temporary
file, flushes and fsyncs it, atomically replaces the registry, and fsyncs the
directory where supported. Reads reject malformed top-level data rather than
silently replacing it with an empty registry.

### Replay manifests

Move manifest sequence allocation into SQLite. A transaction allocates the next
sequence per `(scope, capability)`, and a unique constraint protects
`(scope, capability, manifest_sequence)`. Concurrent creators retry bounded
unique conflicts. L5 readiness treats any historical collision as corruption,
but newly generated packs cannot collide.

## 8. DNS-Rebinding-Safe Intake

The default intake transport resolves the hostname once, rejects loopback,
private, link-local, multicast, reserved, and unspecified addresses, then opens
the socket to the selected validated IP. HTTPS preserves the original hostname
for TLS SNI and certificate verification, and HTTP preserves the original Host
header. Redirects repeat the full validation and pinning process. A response is
rejected if the peer address differs from the validated address.

Tests simulate DNS returning a public address during validation and a private
address on a later lookup; the transport must never perform the second hostname
lookup and must connect only to the validated public address.

## 9. Transactional Immutable Deployment

The installer records the previous `current` target before switching. The
release is not committed until all post-switch gates succeed:

1. plugin registry refresh;
2. systemd daemon reload and required service restarts;
3. loopback `/health` identity match for commit, version, import root, and
   package digest;
4. service-active checks;
5. creation of the current deployment receipt;
6. operational acceptance.

Any failure restores the previous symlink atomically, reloads/restarts the old
services, verifies the old health identity, and preserves the failed release for
diagnosis. `COMMITTED=1` is set only after all gates pass. Backup removal happens
after commit. Tests inject failure after each switch-stage operation and verify
both symlink and running-service rollback commands.

## 10. Verification and Release

Every repair starts with a regression test that reproduces the audited
counterexample. Verification is layered:

1. focused red/green test for each defect;
2. touched-module suites;
3. security, contract, governance, storage, platform, and deployment layers;
4. `compileall`, JavaScript syntax checking, shell syntax checking, and
   `git diff --check`;
5. one final full test-suite run for the complete candidate.

The eight known `1.9.69` failures are part of the candidate: systemd bytecode
cache metadata becomes release-derived rather than hard-coded, and the
`after_tool_call` hook is added to the platform contract.

Only after all local gates pass are `pyproject.toml` and
`eimemory/version.py` advanced together. The branch is committed, pushed,
merged into `master`, and pushed from the authoritative repository. Production
deployment then verifies:

- repository HEAD, release symlink, package version, `/health`, and receipt
  identity all agree;
- RPC refuses unauthenticated RPC while loopback health remains available;
- source-trust and fake-L5 production-safe probes fail closed;
- Feishu delivery state contains no overdue nonterminal item without a valid
  resume path;
- operational acceptance passes;
- L5 either passes with genuine thresholds or truthfully reports
  `data_accumulating` with only sample/type deficits.

The final Feishu notification is sent only after this independent production
recheck and includes version, full commit, deployment identity, L5 state, and
any remaining data-accumulation count. No secret is included.

## Acceptance Matrix

| Requirement | Authoritative proof |
| --- | --- |
| RPC fail-closed | startup tests plus unauthenticated/authenticated production RPC probes |
| Trust cannot self-elevate | attacker-payload regression plus stored policy-digest evidence |
| Fake/stale evidence rejected | nonexistent, wrong-kind, cross-scope, and old-commit tests |
| Real tasks drive L5 | dashboard/readiness tests and current-release production metrics |
| Prompt safety operational | executed case manifest and current-release assessment record |
| Feishu reaches a terminal state | crash-window tests, atomic journal tests, and production state scan |
| Storage is recoverable | outbox crash tests and strict corrupted-stream rebuild tests |
| Registry/sequence races closed | multiprocess registry and concurrent replay allocation tests |
| SSRF rebinding closed | pinned-socket redirect/rebinding tests |
| Deploy rolls back | injected post-switch failure tests plus production receipt/health identity |
| Release complete | one final full suite, clean diff, merged/pushed commit, deployed health, closure recheck, Feishu notification |

## Non-Goals

- Replacing SQLite and JSONL with a new database technology.
- Rewriting the large CLI, scheduler, hooks, or promotion modules unrelated to
  these boundaries.
- Manufacturing synthetic business outcomes to satisfy L5 thresholds.
- Treating service health, operational probes, or status notifications as
  business completion.
