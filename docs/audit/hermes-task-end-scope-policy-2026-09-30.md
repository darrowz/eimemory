# Hermes real-task evidence: producer and scope policy (1.14.24)

Date: 2026-09-30. Approved by the operator (Sheline Douville) in the 1.14.24 cycle brief.

## Decision

Records in `embodied::channel::hermes` count toward release lineage and closure only when they are
genuine real-traffic `hermes.task_end` outcomes with verified provenance. Deploy-case or acceptance
pass rates are never counted as natural recall or task quality.

## What counts

A Hermes outcome counts only if all of the following hold:

1. It was produced by the runtime's `record_terminal` for `hermes.task_end`, after Hermes itself
   completed the turn (`post_llm_call` after the tool loop, not interrupted), or after the model
   called `eimemory_verify_outcome` on the single host-verified turn.
2. The turn holds passed, host-attested `hermes.post_tool_call` receipts that were handed off for
   that exact session and run.
3. `valid_runtime_task_evidence` re-verifies it in the scope that owns it: exact event id, trace,
   session and task type; persisted v2 receipts consumed by that trace; a trusted verification
   policy; receipt signatures; and the same release authority.
4. It is not a rehearsal, carries no `acceptance_generated` flag and no `acceptance_case_id`.

Only then is the item tagged `provenance=host_receipt_verified` and admitted to
`real_task_evidence` (release lineage and closure). The scopes read are the product scope plus
the same operator's `::channel::hermes` sibling (same tenant and agent; operator aliases only).

## What never counts

- Codex terminals (unchanged).
- Live or deploy acceptance traces (`eimemory.live_task_acceptance`, `acceptance_*` markers).
- Diagnostic terminals without receipts (`research.unverified`, success `null`).
- Proactive decisions that were only returned to Hermes. Delivery requires proof of injection.

## Controls

- `EIMEMORY_HERMES_AUTO_TASK_END=0` stops the automatic producer.
- `EIMEMORY_HERMES_CHANNEL_REAL_TASK_EVIDENCE=0` removes Hermes items from lineage/closure.

## Known bias

Hermes receipts only certify passing verifications, so Hermes outcomes are success-only. This is
why the closure thresholds are not relaxed, and why sample count and task-type diversity still
apply.
