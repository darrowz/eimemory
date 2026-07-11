# L5 Evidence Closure Design

## Goal

Audit releases 1.9.10 through 1.9.14 and make L5 status depend only on executed,
verified, reversible evidence. The change must preserve autonomous evolution and
must fail closed when a capability replay, assessment, promotion, or rollback
proof is missing.

## Audit Findings

1. `closure-rehearsal` reports `ok=true` when every weak-capability replay is
   `not_run` because it checks only the replay report wrapper.
2. The rehearsal writes a successful task outcome before replay, skill reuse,
   and rollback have completed, and seeds SOPs as `replay_passed=true` without
   a passing replay.
3. L5 readiness counts all replay records, including `not_run` and failed
   results, and does not require the latest `l5_assessment` to be complete.
4. Patch success metrics treat generic `promote` and `rollback` actions as code
   patch samples and accept a deployed status without verification evidence.

## Design

Add fail-closed evidence summaries to `l5_readiness`: only executed pass/fail
replays enter the denominator; L5 requires at least 10 verified replays, pass
rate at least 0.8, and a latest assessment with `complete=true` and no missing
evidence. Keep raw counts for diagnostics, but never use them for promotion.

Tighten `closure_rehearsal` so weak replay packs must contain executed results
and meet their thresholds. Write the successful outcome and mark SOP replay
status only after replay, skill invocation, rollback, and assessment gates pass.
Failed rehearsals remain useful reports but cannot improve success or skill
reuse metrics.

Tighten capability dashboard patch metrics so only explicit `code_patch`
promotions are samples. A success additionally requires gate, side-effect,
verification, post-deploy health, commit/release identity, and rollback evidence.
Legacy incomplete records remain visible as failed/unverified samples.

## Testing and Release

Use regression tests that fail against 1.9.14, then run the affected L5,
dashboard, replay, promotion, and autonomous-learning layers. Do not run the
full suite. Compile all Python modules, check whitespace, bump the patch version,
push `master`, deploy from `/dev-project/eimemory`, verify both user services and
health endpoints, and rerun live L5 readiness.
