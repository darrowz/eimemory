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
- Block deployment on technical bootstrap failures, not on missing or weak
  business evidence.
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
  configuration, invocation, or code execution failed. Treat this as a
  technical deployment error and stop before switching the current release.

The installer must not translate exit `1` into technical success silently; it
must print a bounded marker that post-switch closure can report.

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
- exit `2` stops before the current symlink switch;
- service-disabled/test deployments skip the online bootstrap;
- protected prior-health snapshots are removed on all exit paths.

Run the focused deployment test files once after all code edits, followed by
Shell syntax, `git diff --check`, and the mandatory Ubuntu deployment-contract
workflow. Do not rerun the full local suite already completed for this release.

## Acceptance criteria

- The repaired installer cannot switch a candidate after a technical bootstrap
  error.
- Missing business evidence cannot prevent a technically healthy deployment.
- A ready dataset produces a predecessor high-water baseline before switching.
- Production health reports the final candidate commit and immutable path.
- Release closure advances past `production_recall_gate`.
- Replay, live acceptance, channel acceptance, closure rehearsal, and
  deployment-bound readiness report their actual final states without
  overstating L5.
