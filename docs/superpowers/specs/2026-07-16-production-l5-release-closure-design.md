# Production L5 Release Closure Design

## Goal

Make L5 closure a reproducible property of the currently deployed immutable release. A release is L5 only when its deployment receipt, live task acceptance, fresh replay evidence, closure rehearsal, and independent readiness report all agree on the same production commit.

## Current Failures

1. `run_autonomous_learning_cycle()` returns enough detail to distinguish an idle run from an attempted but failed learning run, but `scheduler.jobs._run_autonomous_learning()` drops that distinction before reusing the summary in `run_l5_cycle()`.
2. The latest production cycle had `eval_verdict=fail`, so it was an active failure and must remain authoritative. Absence of candidates alone must never be inferred as idle.
3. Historical capability replay manifests were correctly invalidated after their member records were rewritten by the metadata backfill. Digest verification must remain strict; closure must generate a fresh evidence batch instead of trusting or repairing the old digest in place.
4. Deploying a new immutable release does not automatically create the commit-bound deployment receipt and live acceptance required by L5. The manual sequence is easy to omit, which leaves a healthy service at L4.

## Decision

Implement both the producer contract and a fail-closed release-closure orchestrator.

### Autonomous activity contract

The full autonomous-learning producer will return:

- `activity_status`: `idle`, `active`, or `failed`.
- `activity_reason`: a stable machine-readable reason.
- `attempted_candidate_count`: the number of candidate specifications that reached evaluation.

Classification rules:

- `idle` is allowed only when the cycle completed successfully and produced no candidate specifications, evaluation records, candidates, promotions, or explicit gate failure.
- `active` is used when any candidate, evaluation, replay, isolation, safety, or promotion work was attempted, including a failed evaluation.
- `failed` is used for timeout, exception, or an explicitly unsuccessful cycle.
- The scheduler summary must preserve the producer status and the minimum evidence needed to audit it: candidate attempt count, evaluation identifiers and verdict, replay gate result and reason, safety gate result, isolation gate result and blocked reasons, and capability replay manifest identity.
- L5 readiness preserves earlier global readiness only for explicit `idle` or `no_change`. Both `active` and `failed` remain authoritative and fail closed.

Historical records without explicit activity status remain active by default. This avoids retroactively converting real failures into idle runs.

### Release-closure orchestrator

Add one production command that executes these stages in order:

1. Verify the immutable release and persist a deployment receipt for the current commit, using the prior mainline commit as rollback evidence.
2. Run the ten commit-bound live acceptance cases and require all case records to validate against that deployment receipt.
3. Run the L5 closure rehearsal. This creates a fresh capability acceptance execution and fresh replay manifests for `search.discovery`, `research.synthesis`, `operations.uumit`, and `device.control`; historical invalid manifests are not mutated or trusted.
4. Build and persist the final readiness report.
5. Return success only when closure rehearsal reports complete and the final report is exactly `current_stage=L5`, `readiness_score=1.0`, with current-deployment live evidence and verified weak-capability replay evidence present.

The command returns a structured report containing each stage result, current commit/version/release identity, persisted record identifiers, and a single blocked stage/reason when it stops. A failed stage prevents later stages from running.

The existing individual commands remain available for diagnosis. The new command composes them; it does not weaken their validation rules.

## Components

- `eimemory/governance/autonomous_learning.py`: produce explicit activity status from the full cycle.
- `eimemory/scheduler/jobs.py`: preserve activity and gate evidence in the reusable nightly summary.
- `eimemory/governance/l5_loop.py`: consume only explicit idle status and keep active failures authoritative.
- `eimemory/governance/release_closure.py`: orchestrate deployment receipt, live acceptance, rehearsal, and readiness.
- `eimemory/api/runtime.py` and `eimemory/cli/main.py`: expose `learn release-closure`.
- Deployment contract tests and systemd files: keep package and bytecode-cache versions aligned at `1.9.50`.

## Data Flow

```text
full autonomous report
  -> explicit activity classification
  -> scheduler reusable summary
  -> L5 assessment snapshot
  -> latest global readiness selection

immutable deploy + service restart
  -> verified deployment receipt
  -> current-commit live acceptance
  -> fresh weak-capability replay manifests
  -> L5 closure rehearsal
  -> independent readiness report
  -> release accepted or blocked
```

## Failure Handling

- Missing or unknown activity status defaults to active, never idle.
- Failed evaluation, replay, safety, isolation, or promotion gates cannot inherit historical L5.
- Manifest digest mismatch remains a hard replay rejection.
- A deployment receipt must match `/dev-project/eimemory`, `/opt/eimemory/current`, `/health`, version, commit, release path, import root, and package tree digest.
- Live acceptance from an earlier commit cannot certify the current release.
- Release closure stops at the first failed stage and reports its evidence without marking the release L5.

## Tests

Use TDD and observe each regression test fail before implementation.

1. Producer classification tests for true idle, active evaluation failure, active candidate/promotion work, and failed cycles.
2. Scheduler integration tests proving the reusable report carries the activity contract and gate evidence into L5.
3. L5 tests proving explicit idle preserves prior readiness while active and failed runs replace it.
4. Replay persistence test proving a fresh manifest remains trusted after closing and reopening the runtime; mutation still causes digest rejection.
5. Release-closure orchestration tests proving stage order, stop-on-failure behavior, commit binding, and the exact L5 success gate.
6. Layered verification: focused tests, adjacent governance/scheduler/deployment suites, `compileall`, `git diff --check`, then one full test-suite run before release.

## Release and Production Acceptance

- Bump `pyproject.toml`, `eimemory/version.py`, and version-pinned systemd cache paths from `1.9.49` to `1.9.50`.
- Commit on `fix/l5-closure-1.9.50`, push the branch, and fast-forward it into `master` from the clean authoritative honxin checkout at `/dev-project/eimemory` so the dirty local main checkout is not overwritten.
- Push `master`, install the immutable release for the resulting mainline commit, and restart required services.
- Run `learn release-closure` against the live release.
- Run a separate read-only `learn l5-readiness` process after the orchestrator completes.
- Completion requires `/health`, current release link, repository HEAD, deployment receipt, live acceptance, closure rehearsal, and independent readiness to identify the same `1.9.50` commit.

## Non-Goals

- Do not rewrite historical replay manifests.
- Do not inherit live acceptance across commits.
- Do not infer idle from zero candidates.
- Do not enable autonomous code application merely to make readiness pass.
- Do not use the primary `E:\eimemory` worktree for feature edits, and do not discard the local safety branch created while cleaning it.
