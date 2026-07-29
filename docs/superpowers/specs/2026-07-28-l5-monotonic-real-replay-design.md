# Monotonic L5 Maturity with Verified Real Replay

Date: 2026-07-28

## Goal

Make L5 maturity independent of software releases and allow verified replay of
real production tasks to close the L5 business-evidence gate. A deployment may
expose new release-specific validation gaps, but it must not reset or lower
accumulated L5 maturity. L5 maturity may decrease only when a confirmed fatal
regression incident explicitly authorizes the downgrade.

## OpenClaw watchdog boundary

`openclaw-loop-watch` remains installed and operational. It provides the
OpenClaw task-loop observation and bounded stale-lease detection needed by the
current runtime.

The retired gateway restart component acted under resource pressure. It remains
absent from deployment and systemd.
`openclaw-feishu-reply-watchdog` also remains retired and must not become a
second delivery path.

No source, deployment, systemd, test, or runtime-identity removal of
`openclaw-loop-watch` is part of this change.

## Selected maturity model

Use two separate state dimensions:

1. **Observed release health** is recomputed for the currently deployed commit.
   It reports replay, channel, storage, recall, assessment, and runtime gaps.
2. **Accumulated L5 maturity** is a scope-bound, version-neutral monotonic
   checkpoint. Normal reports may advance it, but cannot lower it.

The readiness report keeps `current_stage` as the effective accumulated
maturity for compatibility. It additionally exposes `observed_stage` and
`release_validation` so a new release can be visibly unhealthy without falsely
lowering maturity already earned.

This is preferred over:

- Recomputing maturity from a release-bound evidence window, which resets
  progress after ordinary upgrades.
- Blindly copying the previous release's report, which hides current-release
  failures.

## Version-neutral maturity checkpoint

### Stage ordering

The canonical order is:

`L3.5 < L4 < L4.5 < L5`

Each readiness build first computes the existing evidence result as
`observed_stage`. It then resolves the latest valid maturity checkpoint for the
same tenant, agent, workspace, and user scope.

The effective stage is:

`max(previous checkpoint, observed stage)`

The checkpoint semantic identity contains the scope and maturity schema only.
It contains no version number, commit, deployment receipt, or release session.
Release identity may be attached as provenance describing where advancement
was observed, but it is never part of checkpoint selection.

### Persistence

When `persist=True`:

- an advancement writes a new active maturity checkpoint;
- an equal or lower observation leaves the checkpoint unchanged;
- the report records both observed and effective stages;
- a lower observation emits a visible regression warning instead of silently
  changing the effective stage.

Existing valid persisted L5 readiness reports bootstrap the initial checkpoint
by selecting the highest recognized historical stage in the same scope. The
feature rollout therefore cannot reset existing progress.

## Two valid paths through the real-business gate

L5 business evidence passes when either path succeeds:

1. **Verified live tasks**
   - at least 10 non-rehearsal production outcomes;
   - at least 5 distinct task types;
   - success rate at least 0.8.
2. **Verified real replay**
   - at least 10 independently sourced real production cases;
   - at least 5 distinct task types;
   - replayed by the current code;
   - pass rate at least 0.8.

Operational probes, synthetic fixtures, manually labelled examples, rehearsals,
and duplicated source traces do not count toward either path.

The report exposes a combined `real_business_gate` with:

- `ok`;
- `accepted_path`: `live_tasks`, `real_replay`, or empty;
- the complete live-task and real-replay subreports;
- sample, task-type, pass-rate, and provenance deficits.

The existing `live_task_gate` remains available for compatibility and
diagnostics, but L5 promotion uses `real_business_gate`.

## Verified real replay contract

The existing `real_task_replay.v1` label alone is not sufficient because its
dataset can be supplied manually. A replay qualifies as real only when every
counted case has a verified production provenance chain.

### Required case provenance

Each counted case must reference exactly one source record that:

- exists in the same tenant, agent, workspace, and user scope;
- is an `outcome_trace` or an accepted external-platform delivery outcome;
- has `rehearsal` exactly `false`;
- has a successful terminal production status;
- contains a non-empty task type;
- is not synthetic, seeded, fixture, bootstrap, probe, or manually asserted
  evidence;
- passes record-digest and evidence-contract validation.

The replay case persists:

- the source record ID;
- a stable source-evidence digest;
- task type;
- replay execution ID;
- current code package digest;
- replay verdict and latency.

Raw platform IDs, message text, credentials, and personal identifiers are not
copied into replay evidence.

### Deduplication and thresholds

- A source record may contribute to only one counted replay case.
- Ten replay cases must therefore represent ten distinct source records.
- At least five normalized task types must be represented.
- Empty, `not_run`, malformed, duplicated, or unverifiable cases count as
  rejected, not as successful samples.
- The pass threshold is calculated over all accepted executed cases and must be
  at least 0.8.

### Cross-version behavior

Source production evidence may have been collected under an earlier software
release because L5 maturity is not version-bound. The replay itself must execute
against the current code and persist the current package digest.

An upgrade therefore does not require ten new user tasks. It requires either:

- sufficient live evidence already available under the live path; or
- a fresh execution of the verified real replay dataset against the current
  code.

## Release validation

Release-bound checks remain strict where they protect deployment safety:

- runtime identity and immutable package digest;
- release lineage;
- production recall activation;
- storage migrations;
- current OpenClaw channel health.

Failures appear under `release_validation` as `unverified`, `degraded`, or
`failed`. They do not lower accumulated maturity, but a fatal runtime condition
may block active L5 operation until repaired. Promotion to a higher maturity
stage still requires the relevant safety checks to pass.

## Fatal-regression downgrade contract

A downgrade is accepted only when a persisted `incident` record in the same
scope satisfies all of the following:

- `incident_type` is `l5_fatal_regression`;
- `severity` is `critical`;
- `fatal` is exactly `true`;
- `status` is `confirmed`;
- `target_stage` is a recognized stage lower than the checkpoint;
- `evidence_record_ids` is a non-empty list;
- `confirmed_by` is non-empty;
- the incident is newer than the checkpoint it is downgrading.

The downgrade writes a replacement maturity checkpoint containing the incident
record ID, target stage, confirmer, and evidence references. Ordinary failures,
missing release evidence, unconfirmed incidents, or malformed records cannot
lower maturity.

The report exposes:

- `maturity_transition`: `advanced`, `held`, or `fatal_downgrade`;
- `maturity_checkpoint_record_id`;
- `downgrade_incident_id` when applicable;
- `regression_warning` when observed health is below accumulated maturity.

## Failure handling

- Missing or malformed historical checkpoints fail closed for advancement and
  are ignored for downgrade.
- Unknown stage labels cannot participate in ordering.
- A malformed fatal incident cannot change the checkpoint.
- A replay whose provenance cannot be verified remains visible with a rejection
  reason and cannot close the business gate.
- A current-code replay failure keeps the real replay path closed even if the
  historical source task originally succeeded.
- `openclaw-loop-watch` stale-lease failures remain operationally visible and
  use the bounded `reconcile-stale` repair path.

## Test strategy

Implementation follows test-first development, followed by one consolidated
verification pass.

### Watchdog boundary tests

- Deployment continues installing and enabling `openclaw-loop-watch.timer`.
- Runtime identity continues checking `openclaw-loop-watch.service`.
- Gateway restart automation remains absent.
- The Feishu reply watchdog remains retired and cannot resend.

### Real replay tests

- A manually labelled `real_task_replay` without source provenance is rejected.
- Synthetic, rehearsal, bootstrap, probe, duplicated, malformed, and
  cross-scope sources are rejected.
- Ten unique real sources across five task types with current-code pass rate
  at least 0.8 satisfy the real business gate.
- Nine sources, four task types, or pass rate below 0.8 do not satisfy it.
- Current-code replay execution is required even for older source evidence.
- Live tasks and verified real replay independently satisfy the combined gate.
- No raw platform identifier or message content enters persisted replay
  evidence.

### Monotonic maturity tests

- A software version or commit change cannot lower accumulated maturity.
- A lower observed stage is reported separately while `current_stage` stays at
  the checkpoint.
- A higher observed stage advances the checkpoint.
- A valid confirmed fatal incident can lower the checkpoint.
- Non-critical, unconfirmed, cross-scope, malformed, or evidence-free incidents
  cannot lower it.
- Historical readiness records bootstrap the highest valid stage.
- No checkpoint key or selection rule depends on release version or commit.

## Deployment acceptance

After implementation and consolidated local tests:

1. Commit and push the complete change set.
2. Deploy the exact full commit from `/dev-project/eimemory`.
3. Confirm GitHub master, remote repository HEAD, `/opt/eimemory/current`, and
   `/health` all identify the same commit.
4. Confirm `openclaw-loop-watch` remains enabled and healthy, while gateway
   restart automation remains absent.
5. Confirm the OpenClaw gateway, loopback proxy, eimemory RPC, and systemd failed
   unit set are healthy.
6. Execute the verified real replay dataset on the deployed code and confirm the
   accepted source count, task-type count, pass rate, package digest, and
   rejection set.
7. Confirm L5 does not fall below the pre-deployment maturity checkpoint and
   that the real replay path can close `real_business_gate`.
