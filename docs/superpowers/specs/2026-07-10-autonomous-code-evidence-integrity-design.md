# Autonomous Code Evidence Integrity Design

## Goal

Remove synthetic success evidence from autonomous code evolution. A code patch
may be promoted only after the exact patch is applied in an isolated repository
and at least one declared verification command executes successfully.

## Approved scope

- Keep policy replay case construction as a structural step, but mark it as
  `executed=false`; a case definition is never execution evidence.
- Preflight structured code patches in an isolated Git worktree when possible,
  with a copied repository fallback for non-Git test fixtures.
- Persist the preflight report as a scoped `replay_result` with the base commit,
  patch digest, executed command reports, and a stable evidence record ID.
- Feed the persisted preflight into the isolated evaluator. Do not synthesize
  real-task replay, canary, doctor, smoke, or prompt-safety success.
- Make `promotion_manager` independently obtain or validate canonical preflight
  evidence so callers cannot bypass the protection with a forged gate bundle.
- Require non-empty verification commands both before preflight and at the
  real repository mutation boundary.
- Preserve the existing second verification after applying to the real
  repository, plus commit, deploy, health, rollback, and lifecycle behavior.

## Evidence contract

A successful code preflight contains:

- `report_type=code_patch_preflight`
- `executed=true`, `ok=true`, and `verdict=pass`
- the source repository commit and deterministic patch digest
- a non-empty verification report where every command has `ok=true` and
  `returncode=0`
- a persisted replay-result record ID used by replay, canary, doctor, and smoke
  fields in the promotion gate

Missing, skipped, unavailable, stale, mismatched, or failed evidence blocks the
candidate before the real repository is changed.

## Data flow

```text
bad outcome + structured code patch
  -> structural replay-case validation (not execution)
  -> isolated repo/worktree + apply exact file updates
  -> execute required verification commands
  -> persist code_patch_preflight replay result
  -> isolated evaluator + stop judge
  -> canonical promotion gate built from persisted evidence
  -> apply to real repo + execute verification again
  -> optional commit/deploy/health/canary/rollback lifecycle
```

## Failure behavior

- Missing verification commands: block before sandbox creation.
- Sandbox creation or patch application failure: persist a failed preflight and
  block.
- Any verification failure or timeout: persist command evidence and block.
- Caller-supplied synthetic gate fields: replace with canonical persisted
  preflight evidence.
- Cleanup failure: report it in preflight evidence; do not turn a failed
  verification into success.

## Testing

- RED/GREEN coverage for definition-only replay metadata.
- RED/GREEN coverage for required empty command lists.
- Code patches with failing verification never mutate the real repository.
- Forged gate bundles cannot bypass canonical preflight.
- Successful autonomous code patches expose one evidence ID consistently
  across replay, canary, doctor, smoke, and evaluator records.
- Existing promotion rollback/deploy tests and the full suite remain green.
