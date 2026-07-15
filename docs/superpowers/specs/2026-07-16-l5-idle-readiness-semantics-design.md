# L5 Idle Readiness Semantics Design

## Problem

L5 assessment snapshots currently mix two meanings: the result of the current autonomous-learning run and the last verified global readiness state. An explicitly idle run can therefore become the latest L4 snapshot and hide a previously verified L5 state.

## Decision

Keep every assessment snapshot for audit, but separate activity status from global readiness:

- Normalize explicit `idle` and `no_change` autonomous-learning statuses to `activity_status="idle"`.
- Persist the normalized activity status in assessment content and metadata.
- When the latest snapshot is idle, global readiness resolves to the most recent non-idle assessment.
- When there is no earlier non-idle assessment, remain fail-closed.
- A real active replay or promotion failure remains authoritative and replaces previous L5 readiness.

## Scope

Modify only `eimemory/governance/l5_loop.py`, `eimemory/governance/l5_readiness.py`, the focused L5 tests, and the package version. Do not infer idle status for historical records that lack explicit activity evidence.

## Verification

- An idle snapshot after verified L5 reports local L4 but preserves global L5.
- An idle snapshot without history remains not ready.
- A non-idle failure after verified L5 replaces global readiness.
- Existing insert-order and persistence tests remain green.
- Full test suite passes before deployment.
