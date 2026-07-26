# L5 Capability Lineage And Watchdog Self-Heal Design

## Goal

Keep verified L5 capabilities valid across autonomous patch releases without
allowing stale deployment evidence to certify an unverified runtime. Close the
OpenClaw watchdog loop so expired work is repaired, verified, and terminal
instead of leaving systemd permanently failed.

## Production Failures

The production audit on 2026-07-26 found three connected failures:

1. `/dev-project/eimemory`, `/opt/eimemory/current`, and `/health` identify
   `1.9.88 / 14c59c3`, while the RPC and gateway systemd drop-ins still export
   `EIMEMORY_RUNTIME_COMMIT=100cbb4`. The database has no verified deployment
   receipt newer than `1.9.86`.
2. The missing current release identity prevents terminal events, proactive
   recall decisions, replay evidence, and readiness from binding to the live
   release. L5 therefore falls to L4 even though the runtime itself remains
   healthy.
3. `openclaw-loop-watch` detects expired work every five minutes but does not
   execute a repair. Six old active tasks have caused every watchdog run in the
   last 72 hours to fail.

## Considered Approaches

### A. Verified deployment plus capability lineage

Require an exact current deployment receipt, then reuse capability evidence
when the relevant capability contract digest is unchanged. Changes to a
capability domain require only that domain's release gate to run again.

This is the selected design. It preserves evidence across ordinary autonomous
version bumps without weakening runtime identity.

### B. Rebuild all evidence after every commit

This keeps exact commit binding but makes autonomous evolution impractical:
real-task accumulation and expensive replay restart after documentation,
packaging, or unrelated adapter changes.

### C. Remove release binding from L5

This would keep the displayed stage stable but permit old evidence to certify
unknown code. It is rejected because it breaks rollback, source isolation, and
deployment accountability.

## Invariants

1. The live code commit, systemd `EIMEMORY_RUNTIME_COMMIT`, immutable release
   directory, `/health`, and current deployment receipt must agree exactly.
2. A capability result is reusable only when its scope, evidence digest, source
   contract, and capability-domain digest remain valid.
3. Unknown or unclassified source changes invalidate the affected capability
   domain. They never inherit by default.
4. Channel and source authority boundaries remain exact. Lineage cannot move
   evidence between OpenClaw, Codex, Hermes, tenants, users, workspaces, agents,
   or sources.
5. Historical records are immutable. Continuity is represented by a new
   current-release attestation that references prior evidence; old records are
   never rewritten.
6. An autonomous release can preserve L5 only after its immutable deployment
   receipt and all required changed-domain gates pass.

## Capability Lineage Contract

Introduce a release-bound capability lineage attestation with:

- current and prior verified deployment receipt identities;
- an ancestry check proving the prior commit is an ancestor of the current
  commit;
- a stable digest for each L5 capability domain;
- the changed paths used to compute domain impact;
- inherited evidence references for unchanged domains;
- current-release gate references for changed domains;
- a final `compatible` decision that fails closed when any input is missing.

The initial domains are:

- `memory.recall`: retrieval, indexing, embeddings, recall policy, and the
  storage queries used by recall;
- `memory.governance`: outcome traces, replay manifests, promotion, rollback,
  readiness, and evidence contracts;
- `channel.openclaw`: OpenClaw hooks, runtime adapter, prompt injection, and
  terminal-event correlation;
- `storage.integrity`: SQLite schema, migrations, payload segments, and
  release storage transactions;
- `deployment.runtime`: immutable installer, systemd templates, health
  identity, and receipt recording.

Domain digests are computed from Git tree entries for explicit path sets.
Version files, tests, documentation, and changelogs do not change a capability
digest. Any production file outside the classifier marks all domains changed.

Readiness continues to resolve the exact current deployment identity. It may
then accept a prior capability result through the current lineage attestation:

```text
current immutable receipt
  + verified ancestor receipt/evidence
  + unchanged domain digest
    -> inherited current-release capability evidence

changed domain
  + current-release replay/smoke/acceptance gate
    -> refreshed current-release capability evidence
```

This separates deployment truth from capability continuity: deployment is
always commit-exact, while capabilities survive compatible versions.

## Autonomous Release Flow

Every autonomous patch release must execute:

1. classify changed paths and compute capability-domain digests;
2. install the immutable release;
3. regenerate every managed runtime-commit drop-in from the candidate commit;
4. restart services and verify health identity;
5. persist the exact current deployment receipt;
6. run gates for changed domains;
7. persist the lineage attestation;
8. run readiness using exact current identity plus lineage evidence;
9. accept the release, or roll back if a critical changed-domain gate fails.

The deployment installer must render drop-ins atomically and verify their
effective systemd environment after `daemon-reload`. A mismatch blocks receipt
creation and release acceptance.

## Watchdog Self-Heal Flow

`openclaw-loop-watch` uses a bounded two-phase repair:

1. Detect stale active tasks and create or reuse one auditable repair task.
2. Respect a configurable safety grace after lease expiry so a long-running
   turn is not failed immediately.
3. Reconcile eligible stale tasks to `failed` with
   `lease_expired_reconciled`; never manufacture passing task evidence.
4. Run `find_stale_tasks()` again.
5. Record a repair action and verification containing only bounded counts and
   reason classes.
6. Finish the repair task as `done` only when drift is healthy and no stale
   tasks remain. Otherwise finish it as `blocked` and keep the watchdog
   non-zero.
7. Persist one final watchdog record describing pre-repair count,
   reconciled count, remaining count, and repair-task outcome.

Old blocked watchdog findings are terminal diagnostics and are not themselves
treated as stale active work.

## Observation Gate

The 48-hour observation gate distinguishes a healthy business block from an
execution failure:

- malformed readiness, missing runtime identity, failed commands, storage
  errors, or failed services remain non-zero systemd failures;
- a valid readiness report below L5 persists a structured
  `observation_pending` result and exits successfully;
- autonomous code apply remains disabled until exact L5 is reached;
- the timer remains scheduled for a later bounded recheck instead of becoming
  permanently elapsed and failed.

This removes monitoring noise without pretending that a blocked release is L5.

## Failure Handling

- Runtime-commit mismatch blocks the release before a receipt is written.
- Missing current receipt blocks lineage creation.
- Broken Git ancestry, missing prior evidence, digest mismatch, or unknown
  paths invalidate inheritance.
- A failed changed-domain gate prevents the lineage attestation from becoming
  compatible.
- A watchdog repair that leaves stale work returns non-zero and exposes bounded
  evidence.
- LLM availability is irrelevant to all identity, lineage, watchdog, and
  readiness decisions.

## Verification

All behavior changes use test-first red-green cycles.

1. Watchdog tests prove repair-task creation, grace handling, bounded
   reconciliation, second-pass verification, idempotency, and failure when
   stale work remains.
2. Deployment tests prove every managed drop-in receives the candidate commit
   and that effective-environment mismatch blocks release acceptance.
3. Evidence-contract tests prove compatible domains inherit across descendant
   patch releases, changed domains require fresh evidence, unknown paths fail
   closed, and cross-scope/channel evidence is rejected.
4. Readiness tests prove a compatible lineage preserves L5, while a missing
   current receipt, broken ancestry, or incomplete changed-domain gate
   downgrades it.
5. Observation-gate tests prove valid below-L5 readiness is pending rather than
   a failed unit, while operational errors remain failures.
6. Release verification includes focused tests, adjacent governance and
   deployment suites, compileall, `git diff --check`, static review, immutable
   production deployment, receipt verification, watchdog recovery, live
   acceptance, replay, and an independent readiness read.

## Release Acceptance

The patch release is complete only when:

- local master, GitHub `origin/master`, `/dev-project/eimemory`,
  `/opt/eimemory/current`, systemd runtime commit, `/health`, tag, and deployment
  receipt identify the same commit and version;
- pending storage migrations are empty;
- the six production stale tasks are reconciled through the new repair flow;
- the watchdog timer's latest run succeeds and user failed units are empty;
- the lineage attestation is valid for the current release;
- readiness reports the evidence it inherited and the gates it reran without
  claiming L5 from missing data;
- the result notification is accepted by the production Feishu API.

## Non-Goals

- Do not make a version string alone an evidence authority.
- Do not copy historical records to a new commit without a lineage
  attestation.
- Do not auto-pass changed capability domains.
- Do not auto-reconcile tasks before the safety grace expires.
- Do not enable autonomous code deployment merely to make the observation gate
  green.
