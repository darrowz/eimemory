# Pre-switch Production Recall Baseline Design

## Context

The immutable deployment transaction correctly separates technical release
commit from post-deployment business validation. However, commit `4ba474e`
removed the installer call to the existing
`deploy/bootstrap_production_recall.py` helper.

The production recall gate requires an independently observed predecessor
baseline. Once `/opt/eimemory/current` has switched to the candidate, the
candidate cannot legally create that predecessor baseline for itself. A
post-switch retry therefore remains blocked with
`prior_release_baseline_high_water_missing`, even when the production query
dataset is otherwise eligible.

## Goals

- Capture predecessor recall evidence while the predecessor is still running.
- Keep technical deployment fast and independent from business-data maturity.
- Keep L5 evidence collection and readiness independent from RPC, storage,
  recall, OpenClaw, and immutable-release availability.
- Preserve strict anti-self-blessing rules in the production recall gate.
- Leave post-switch business validation non-rollback and independently
  retryable.

## Non-goals

- Do not relax production recall thresholds or baseline trust rules.
- Do not fabricate production queries, labels, or channel acceptance.
- Do not reintroduce storage snapshots when no storage migration is pending.
- Do not make an incomplete L5 dataset appear accepted.

## Design

### Pre-switch phase

After the candidate release has been built and validated, but before the
`/opt/eimemory/current` symlink changes, the installer will:

1. Capture the running predecessor `/health` response into the existing
   protected snapshot format.
2. Run `deploy/bootstrap_production_recall.py` as the service user against the
   candidate code and predecessor runtime.
3. Remove the protected snapshot on every success or failure path.

This phase remains online and non-destructive. It does not stop storage writers
and is independent of whether a storage migration is pending.

### Outcome classification

The existing bootstrap helper exit contract is retained:

- Exit `0`: baseline anchor is ready, an existing baseline is reusable, or the
  dataset is legitimately accumulating. Continue deployment.
- Exit `1`: business evidence is not ready or does not pass its business gate.
  Emit a degraded/pending marker and continue technical deployment.
- Exit `2` or another unexpected nonzero status: protected snapshot,
  configuration, invocation, or L5 bootstrap code execution failed. Emit a
  bounded L5 bootstrap error and continue technical deployment.

The installer must not translate any degraded result into L5 success silently;
it must print a bounded marker that post-switch closure can report. It also must
not translate an L5-only failure into a core deployment failure.

### L5 isolation boundary

L5 is an observational evidence and promotion subsystem. It is not a
prerequisite for the product runtime.

- Core deployment blockers are limited to release construction, dependency
  integrity, source and runtime identity, storage migration safety, service
  restart, and health verification.
- Production recall baseline, weak-capability replay, live acceptance, channel
  acceptance, closure rehearsal, and readiness may independently report
  `pending`, `degraded`, or `blocked`.
- L5 state must not change `/health` process or store readiness, invalidate a
  committed deployment receipt, stop RPC or OpenClaw, or trigger rollback.
- Improvements to core memory, recall, deployment, or channel functionality are
  accepted by their own contracts even when the formal L5 evidence dataset is
  incomplete.
- A separate strict L5 verification command may return nonzero for automation
  that explicitly requests L5 promotion. That exit status is not reused as the
  immutable installer exit status.

### Post-switch phase

The current technical transaction remains unchanged:

1. Atomically switch the current symlink.
2. install runtime metadata and restart services;
3. verify deployed commit, version, release path, and health;
4. mark the technical transaction committed;
5. run deployment receipt, release lineage, and business closure.

Business closure failure remains non-rollback. If the pre-switch phase produced
an anchor, the current production recall gate compares the candidate against
that predecessor evidence. If it produced a pending record, closure reports the
specific data or metric blocker.

## Recovery for the current release

The currently deployed candidate cannot retroactively produce a trusted
predecessor baseline. After the fix passes review and CI:

1. Switch temporarily to the existing immutable predecessor
   `a2c35601dcd4737ac315a1d6a355c9d4bbc27a6d` with post-switch business gates
   disabled.
2. Verify predecessor commit, current link, and health.
3. Deploy the fixed candidate through the repaired installer.
4. Let the repaired pre-switch phase capture the predecessor baseline.
5. Verify the candidate deployment and run the full release closure sequence.

The temporary switch is bounded to existing immutable releases and does not
modify storage.

## Tests

Add installer contract tests that prove:

- prior health capture and recall bootstrap occur before the current symlink
  switch;
- the bootstrap runs even when no storage migration is pending;
- exit `0` continues normally;
- exit `1` records degraded business evidence and continues;
- exit `2` records an L5 bootstrap error and still allows a technically healthy
  current symlink switch;
- service-disabled/test deployments skip the online bootstrap;
- protected prior-health snapshots are removed on all exit paths.

Add decoupling tests that prove an L5 bootstrap, closure, or readiness failure
does not alter core health, deployment receipt validity, RPC availability, or
the installer success status after technical commit.

Run the focused deployment test files once after all code edits, followed by
Shell syntax, `git diff --check`, and the mandatory Ubuntu deployment-contract
workflow. Do not rerun the full local suite already completed for this release.

## Acceptance criteria

- Missing business evidence or an L5-only execution error cannot prevent a
  technically healthy deployment.
- A ready dataset produces a predecessor high-water baseline before switching.
- Production health reports the final candidate commit and immutable path.
- Release closure advances past `production_recall_gate`.
- Replay, live acceptance, channel acceptance, closure rehearsal, and
  deployment-bound readiness report their actual final states without
  overstating L5.
- RPC, storage, recall, OpenClaw, and deployment health remain independently
  usable when the final L5 state is below L5.
