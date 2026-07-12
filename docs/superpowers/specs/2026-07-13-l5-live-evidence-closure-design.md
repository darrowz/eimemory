# eimemory 1.9.24-1.9.25 L5 Live Evidence Closure Design

## Objective

Close the remaining semantic gap between a successful governance rehearsal and
a production L5 claim.  The implementation must remain reproducible, fail
closed, and must not reinterpret legacy unverified outcomes as successful live
tasks.

## Evidence classes

The dashboard keeps `task_success_rate` as a broad diagnostic over historical
task-like outcomes.  A new `verified_live_task_success_rate` is computed only
from trusted `outcome_trace.v1` records that meet all of these conditions:

- `outcome.rehearsal` is exactly `false`;
- `outcome.success` is an explicit boolean;
- `verifier.passed` is an explicit boolean;
- the verifier has a non-empty evidence reference;
- the referenced evidence exists in the same scope and is trusted;
- the trace has a non-empty task type and a trusted recorder source.

Production acceptance traces must reference a current-deployment-bound
acceptance case and its verified deployment receipt. Generic OpenClaw terminal
events remain in the broad diagnostic until their event/outcome/verifier chain
has an equally strict contract; they cannot grant L5. A verified acceptance
failure remains in the denominator; only an explicit successful outcome with a
passing verifier contributes to the numerator.

## Production live acceptance

`eimemory learn live-acceptance` executes ten read-only production tasks across
storage, recall, source registry, policy ledger, skills, dashboard, readiness,
replay integrity, deployment receipt, and live health identity.  Each task:

1. runs against the deployed runtime rather than a fixture;
2. persists a compact acceptance case containing only booleans, counts,
   deployment identity, and an observation digest;
3. records a non-rehearsal outcome trace whose verifier references that case;
4. is idempotent by deployment commit and case id.

The command accepts only the canonical repository, current symlink, and
loopback health endpoint.  It fails closed unless the live commit, version,
release path, import root, package digest, and prior deployment receipt agree.

## L5 gate

The existing replay, weak-capability, assessment, promotion, rollback, and patch
quality gates remain mandatory.  L5 additionally requires:

- at least 10 strictly verified current-deployment live task outcomes;
- `current_deployment_live_task_success_rate >= 0.8`;
- at least 5 distinct live task types.

When the governance rehearsal is complete but live evidence is insufficient,
the readiness stage is `L4.5`, the readiness score is capped below 1.0, and the
report names the missing live evidence.  The rehearsal report can prove its own
closure without claiming production L5.

## Read-only guarantee

`build_l5_readiness_report(..., persist=False)` must not attribute outcomes or
append any record.  Outcome attribution remains an explicit learning-job side
effect, never a reporting side effect.

L5 assessments are append-only snapshots. Readiness selects the highest SQLite
insertion `rowid`, whose allocation is serialized across runtime connections,
rather than relying on second-resolution timestamps or random record ids.

## Deployment discovery verification

The immutable installer keeps discovering user services that execute from
`/opt/eimemory/current`, but behavior tests must execute the installer path and
prove dynamic discovery, deduplication, and rejection of symlink/non-regular
unit files.  Text-only assertions are insufficient.

## Verification boundary

Run focused and associated tests only; do not rerun the full suite.  Before the
L5 claim, verify local and remote test layers, repository/health/release commit
identity, a fresh deployment receipt, live acceptance results, readiness L5,
service health, clean worktrees, and absence of bytecode in the immutable
release source tree.
