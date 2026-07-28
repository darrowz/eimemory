# Watchdog Retirement and Monotonic L5 Maturity

Date: 2026-07-28

## Goal

Permanently retire the scheduled `openclaw-loop-watch` watchdog and make L5
maturity independent of software releases. A deployment may expose new
release-specific validation gaps, but it must not reset or lower accumulated L5
maturity. L5 maturity may decrease only when a confirmed fatal-regression
incident explicitly authorizes the downgrade.

## Selected approach

Use two separate state dimensions:

1. **Observed release health** is recomputed for the currently deployed commit.
   It reports replay, channel, storage, recall, assessment, and real-task gaps.
2. **Accumulated L5 maturity** is a scope-bound, version-neutral monotonic
   checkpoint. Normal reports may advance it, but cannot lower it.

The public readiness report keeps `current_stage` as the effective accumulated
maturity for compatibility. It additionally exposes `observed_stage` and
`release_validation` so a new release can be visibly unhealthy without falsely
lowering the maturity already earned.

This is preferred over:

- Recomputing maturity from all historical evidence on every request, which can
  fluctuate when evidence limits or retention change.
- Copying the previous release's report into every new release, which hides the
  difference between accumulated maturity and current-release health.

## Watchdog retirement

The automatic `openclaw-loop-watch` execution path is removed from:

- `deploy/install_immutable_release.sh`
  - storage-writer unit inventory;
  - unit installation and timer enablement;
  - runtime-identity verification.
- `deploy/discover_python_runtime_units.sh`.
- `deploy/systemd/openclaw-loop-watch.service`.
- `deploy/systemd/openclaw-loop-watch.timer`.
- systemd documentation and deployment tests that treat the watchdog as an
  installed runtime component.

Deployment gains an idempotent retirement step for legacy hosts:

1. Disable and stop `openclaw-loop-watch.timer`.
2. Stop `openclaw-loop-watch.service`.
3. Remove the legacy service, timer, and known drop-ins.
4. Reload the user systemd manager and clear the retired unit's failed state.

The bounded `openclaw_loop.py reconcile-stale` command remains available for
explicit incident repair. It is not scheduled or included in release identity.

## Version-neutral maturity checkpoint

### Stage ordering

The canonical order is:

`L3.5 < L4 < L4.5 < L5`

Each readiness build first computes the existing release-specific result as
`observed_stage`. It then resolves the latest valid maturity checkpoint for the
same tenant, agent, workspace, and user scope.

The effective stage is:

`max(previous checkpoint, observed stage)`

The checkpoint semantic identity contains the scope and maturity schema only.
It contains no version number, commit, deployment receipt, or release session.
Release identity may be attached as provenance describing where an advancement
was observed, but it is never part of checkpoint selection.

### Persistence

When `persist=True`:

- an advancement writes a new active maturity checkpoint;
- an equal or lower observation leaves the checkpoint unchanged;
- the readiness report records both the observed and effective stages;
- a lower observation emits a visible regression warning instead of silently
  changing the effective stage.

Existing valid persisted L5 readiness reports bootstrap the initial checkpoint
by selecting the highest recognized historical stage in the same scope. This
prevents the feature rollout itself from resetting existing progress.

### Release validation

Release-bound evidence remains strict where it protects deployment safety.
Failures are reported under `release_validation`, including:

- release lineage and runtime identity;
- current-release production recall state;
- storage migrations;
- replay and assessment compatibility;
- current-release real-task samples.

These fields can become `unverified`, `degraded`, or `failed` after an upgrade,
but do not lower accumulated maturity. Promotion to a higher maturity stage
still requires the existing evidence gates to pass.

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
missing current-release evidence, unconfirmed incidents, or malformed records
cannot lower maturity.

The report exposes:

- `maturity_transition`: `advanced`, `held`, or `fatal_downgrade`;
- `maturity_checkpoint_record_id`;
- `downgrade_incident_id` when applicable;
- `regression_warning` when observed health is below accumulated maturity.

## Failure handling

- Missing or malformed historical checkpoints fail closed for advancement and
  are ignored for downgrade.
- Unknown stage labels cannot participate in ordering.
- A malformed fatal incident is reported as rejected evidence and cannot change
  the checkpoint.
- Legacy watchdog units may already be absent; retirement remains successful
  and idempotent.
- Stale OpenClaw leases are not silently repaired by deployment. They remain an
  explicit operational action with before/after evidence.

## Test strategy

Implementation follows test-first development, then one consolidated
verification pass.

### Watchdog tests

- Installer no longer installs, enables, inventories, or identity-checks
  `openclaw-loop-watch`.
- Legacy retirement disables, stops, removes, reloads, and clears failed state.
- Re-running retirement succeeds when files and units are already absent.
- The two systemd unit assets are absent.

### L5 tests

- A software version or commit change cannot lower accumulated maturity.
- A lower observed stage is reported separately while `current_stage` stays at
  the checkpoint.
- A higher observed stage advances the checkpoint.
- A valid confirmed fatal incident can lower the checkpoint.
- Non-critical, unconfirmed, cross-scope, malformed, or evidence-free incidents
  cannot lower it.
- Historical readiness records bootstrap the highest valid stage.
- Current-release validation failures remain visible after maturity is held.
- No checkpoint key or selection rule depends on release version or commit.

## Deployment acceptance

After implementation and consolidated local tests:

1. Commit and push the complete change set.
2. Deploy the exact full commit from `/dev-project/eimemory`.
3. Confirm GitHub master, remote repository HEAD, `/opt/eimemory/current`, and
   `/health` all identify the same commit.
4. Confirm the watchdog unit files are absent, its timer is not enabled, and it
   is absent from runtime identity checks.
5. Confirm `systemctl --user --failed` is empty and the OpenClaw gateway,
   loopback proxy, and eimemory RPC remain healthy.
6. Confirm L5 remains at least the pre-deployment maturity checkpoint while the
   new release's validation and real-data deficits are reported separately.
