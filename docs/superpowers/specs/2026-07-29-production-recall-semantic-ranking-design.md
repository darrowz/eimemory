# Production Recall Semantic Ranking Identity

Date: 2026-07-29

## Goal

Prevent semantically identical ground-truth behavior rules from failing the
production recall gate merely because retrieval returns a different immutable
record instance than the operator-labelled instance.

## Root cause

Production contains multiple active `ground_truth_behavior_rule` records whose
behavior contracts are identical. Their immutable `record_id`,
`lesson_record_id`, and `replay_record_id` differ because each record belongs to
its original correction/replay provenance chain.

The production recall evaluator currently uses physical `record_id` for both
relevance and predecessor-result stability. Retrieval therefore treats an
equivalent rule instance as irrelevant and reports false MRR, NDCG, top-1, and
Jaccard regressions. Dataset construction deduplicates only `case_id`; it has no
stable semantic ranking identity for a labelled ground-truth rule.

## Selected design

Keep exact record identity and trusted operator label evidence unchanged.
Derive a separate `ground_truth_behavior_semantic.v1` ranking identity only for
active rule records whose report type is `ground_truth_behavior_rule`.

The semantic projection contains the record kind, source partition, title,
summary, detail, and complete content except the provenance-only
`lesson_record_id` and `replay_record_id` pointers. The projection is hashed
with the existing stable digest helper and persisted only as a prefixed digest.
All other record kinds continue using exact `record_id`.

During evaluation:

1. Hydrate and validate every exact operator label exactly as before.
2. Project labelled, returned, and predecessor result records to ranking refs.
3. Deduplicate ranking refs in retrieval order.
4. Compute relevance and stability metrics from ranking refs.
5. Preserve exact label and returned IDs in the report for auditability.
6. Persist the ranking refs and ranking result digest so independent report
   validation can recompute the metrics without raw rule text.

This design does not merge, retire, delete, or rewrite historical rules. It
does not infer new operator labels and does not weaken scope, source, channel,
label-evidence, leakage, latency, memory, or release-lineage gates.

## Rejected alternatives

- Expanding one exact operator label to every duplicate record would invent
  label provenance for records the operator did not label.
- Selecting one canonical physical record would still fail when retrieval
  returns another equivalent immutable instance.
- Deleting or superseding duplicate historical rules would destroy provenance
  and is outside this repair.

## Verified-real replay boundary

Verified-real replay remains independent of production recall. A source counts
only when the existing `validate_real_replay_source` contract accepts its exact
immutable `source_record_id`. Existing samples with missing IDs are not
backfilled or inferred.

The production audit found zero valid sources: 294 active outcome traces were
checked, with 221 rejected as `untrusted_terminal_source`, 57 as
`terminal_contract_digest_missing`, and 16 as `unsuccessful_source`. The gate
therefore remains fail-closed at 0/10 until new trusted terminal outcomes are
recorded by the existing OpenClaw/Codex/Hermes accumulation path.

## Tests

- A strict integration regression creates two immutable rules with identical
  behavior and different lesson/replay pointers, labels the first, returns the
  second, and uses the first in the predecessor baseline.
- Before the fix the gate fails on exact-ID MRR/top-1 drift.
- After the fix semantic ranking metrics pass while exact IDs remain visible.
- A second regression changes an actual behavior field and proves it does not
  collapse into the labelled semantic identity.
- Existing real-query, dataset, replay, readiness, release-closure, and version
  tests remain green.

## Deployment acceptance

Release `1.9.112` must be committed, pushed, installed through the immutable
release script, and verified by matching repository, current symlink, status,
and `/health` commit/version identities. Production acceptance runs health,
`l5-assess`, `capability-replay`, `release-closure`, and `l5-readiness`.

L5 structural completion and production recall may close after this repair.
Verified-real replay must remain visibly gated if fewer than 10 valid immutable
sources across 5 task types exist.
