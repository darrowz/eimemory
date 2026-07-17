# L5 Evidence Quality Design

## Goal

Increase L5 evidence quality without relaxing readiness. Measure verified real
OpenClaw tasks and identify the layer most likely responsible for failures.

## Scope

- Add deterministic `blame_layer` attribution to `outcome_diagnosis.v1`.
- Hoist the layer into outcome-trace metadata.
- Add separate verified real-task dashboard metrics and failure breakdowns.
- Keep current-deployment acceptance and L5 readiness unchanged.

## Contracts

Stable layers are `planner`, `tool`, `memory`, `device`, `verifier`, `operator`,
and `unknown`. Existing diagnosis labels and signals map deterministically;
unclassified evidence stays `unknown`.

A verified real task must be an `outcome_trace.v1`, non-rehearsal trace with a
task type, matching boolean outcome/verifier results, and one allowlisted
OpenClaw terminal event. The event and its outcome must be in the same scope and
bind the trace ID, session, task type, hook method, verdict, and trusted outcome
source. Each terminal event counts once. These samples never count as
current-deployment acceptance.

## Verification

Unit tests cover each blame layer, metadata persistence, valid business
evidence, forged evidence exclusion, rehearsal exclusion, and isolation from
the L5 deployment gate. Release verification includes the full suite, push,
immutable deployment, runtime identity, production metrics, and health.
