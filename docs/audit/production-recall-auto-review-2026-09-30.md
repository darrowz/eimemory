# Production recall label auto-review (1.14.16)

Date: 2026-09-30
Decision: recall relevance labels are auto-reviewed. A pending
production-query case no longer needs a human operator to run
`eimemory eval production-query accept` when independent evidence agrees.

## What already existed and is reused

The label pipeline is unchanged:

`pending_case` (`eimemory.production_recall.pending_case`) → signed label
evidence (`eimemory.production_recall.label_evidence`, HMAC over the
`receipt_key_set`) → `accepted_case` (`eimemory.production_recall.accepted_case`)
→ `build_production_query_dataset` → `real_query_gate` (freeze → hydrate →
evaluate) and `deploy/bootstrap_production_recall.py`.

The auto-reviewer writes to that same pipeline. The only difference is the
label authority. The 1.14.12 retirements stay in place: the rank-heuristic
`auto_label_proposals`, the `delegated_recall_review` reviewer and the
`delegated_ai` authority remain rejected. Retrieval membership or rank alone is
still never label authority (`tests/test_retired_recall_workflow.py` is
untouched and passes).

## Authority model

| authority | labeler | evidence_class | signature schema |
|---|---|---|---|
| human | `operator` | `operator_relevance_label` | operator authority (1.13.25) |
| auto | `auto_review` | `auto_review_relevance_label` | `production_recall_auto_review_label.v1` |

Every auto-accepted label carries:

- label evidence meta `label_authority=auto_review` and `auto_review_packet_digest`
- a packet (`production_recall_auto_review_packet.v1`) with `criteria_version`
  (`production-recall-auto-review.v1`), `reviewer=eimemory.auto_review`,
  `pending_record_id`, `inputs_digest`, per-ref signals, `query_features_origin`
  and `reviewed_at`
- an HMAC signature from the same receipt key set as the other production
  recall evidence. It is verified again at dataset build, repair and gate hydration.
- a review receipt (`eimemory.production_recall.auto_review`, record id
  `prar_…`) for **every** reviewed case (accepted, pending or rejected) with
  the reasons and the inputs digest. The receipts are idempotent: the same
  inputs are not written twice.

## Criteria v1 (deterministic)

For each candidate ref captured in the pending case (only captured refs, never
new ones):

- **S_sem**: a validated `semantic-relevance.v1` observation for the exact
  delivered decision (identity, digests and verdict re-parsed) marks the
  delivered item `relevant`.
- **S_proof**: the item's render evidence is `verified-parent-span.v1` (1.14.14
  verifier) for this record, and its `record_digest` equals the record's
  current digest.
- **S_used**: the item was delivered and the host marked it `used`.

A ref is labeled relevant only when **S_sem AND (S_proof OR S_used)**. The
grade is 3 when all three signals hold, otherwise 2. The case is accepted only
if at least one ref qualifies **and** redacted query features can be derived
from the private original-query vault (at most 8 distinct informative tokens,
with stopwords, digits, ID-like tokens and CJK runs over 8 chars dropped). The
features must pass the same redaction and quality checks as an operator
packet.

Outcomes:

- **accepted**: labels are minted through `accept_auto_reviewed_production_query`.
- **pending** (with reason): `no_candidate_refs` (an empty result is never
  auto-certified as a true no-answer), `no_candidate_delivered`,
  `semantic_judgment_missing|unknown|not_delivered`,
  `independent_signal_agreement_missing`, `query_features_*`,
  `original_query_*`, `auto_review_revoked`, and transient capture authority
  reasons.
- **rejected** (recorded only, the pending record itself is not modified):
  capture validation failures, `semantic_off_topic`,
  `host_rejected_all_candidates`.

The reviewer never accepts without evidence. With no signing key it refuses to
write (`auto_review_attestation_key_unavailable`). A dry run works without
the key.

## Policy flag

`EIMEMORY_PRODUCTION_RECALL_AUTO_REVIEW` defaults to enabled. Any of
`0/false/no/off/disabled` turns it off. When it is off:

- `auto_review` is not a trusted labeler. Auto cases are excluded from the
  dataset (`auto_review_policy.excluded_by_policy`), frozen gate cases and
  hydration.
- repair does not treat excluded auto cases as a conflict.

The 15-label threshold is unchanged. Human and auto labels count toward the
same threshold. Human cases are ordered first.

## Where it runs

- nightly: step `production_recall_auto_review` runs immediately before
  `production_recall` (the recall quality gate). Report key
  `production_recall_auto_review`. Diagnostics key
  `recall_label_auto_review` (accepted/pending/rejected, reason counts,
  accepted-by-authority).
- deploy bootstrap (`deploy/bootstrap_production_recall.py`): runs after
  collect and before build, reported as `auto_review`. It never blocks the
  bootstrap.
- CLI: `eimemory eval production-query auto-review [--dry-run] [--channel] [--limit]`
  and `eimemory eval production-query auto-review-revoke <pending_id> --reason <code> --revoked-by <actor>`.
- the gate: eligibility reports `label_authority_counts{human,auto_review}` and
  `auto_review_labels_enabled`. Dataset progress reports
  `accepted_by_authority` / `accepted_labels_by_authority`.

## Revocation

`auto-review-revoke` appends a signed revocation receipt (meta
`auto_review_revoked_pending`). After that the accepted auto case fails
validation (`auto_review_label_revoked`) in the dataset build, gate and
repair, and the reviewer will not accept that pending case again. Human
labels are not affected. Nothing is deleted, so the audit trail is kept.

## Trust tradeoff

- The semantic judge (LLM) and the 1.14.14 verifier are different mechanisms,
  but both look at the same delivered text, so their errors may be correlated.
  Host `used` feedback is the most independent signal. Grade 3 therefore
  requires all three signals.
- Unlabeled captured candidates in an accepted auto case count as
  non-relevant, the same as in an operator case. That can under-count recall
  when the judge missed a relevant item. The error is conservative for the
  gate: the result goes down, not up.
- Auto labels can be told apart from human ones everywhere (authority, packet,
  receipts). They can be switched off with one flag and revoked one case at a
  time.
- Privacy: only redacted keyword terms are stored in the label packet, never
  the original query text.

## Estimate on honrui evidence (read-only probe, 2026-09-30)

Probe of the honrui store copy (no writes): 160 active pending cases, 53 in
`hongtu/embodied::channel::hermes/darrow`.

- 139/160 pending cases have 0 candidate refs, so they stay pending (`no_candidate_refs`).
- All 44 decision items attached to the rest were never injected
  (`not_used`/`suppressed`/`volunteered`). There are 0 `used` states, 0
  semantic observations and 0 `verified-parent-span` render evidence (those
  decisions are older than 1.14.13/1.14.14).
- All task types are `research.task`. The semantic monitor currently only
  evaluates `memory.recall` decisions.

**Estimate: 0 of 53 would be auto-accepted, so this alone does not reach 15.**
Auto-review removes the human bottleneck but cannot manufacture evidence.
Reaching the threshold needs delivered recall decisions made after 1.14.14
(with verified spans) that also get semantic observations and/or host `used`
feedback in the channel scope.

## Related fix

`production_query_repair._validate_label` required the exact six-key label
payload. Operator labels signed since 1.13.25 add `operator_authority`, so any
operator-accepted label produced `label_schema_mismatch`, and the deploy
bootstrap blocked with `production_query_scope_repair_conflict`. Repair now
accepts the signed operator payload and validates auto labels through their
own signature path.
