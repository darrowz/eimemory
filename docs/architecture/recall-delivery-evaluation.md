# Recall evaluation in delivered order

The evaluator must score the order returned by the retrieval call. Relevance
labels identify correct results; they cannot select a result channel, move a
labelled rule to the front, replace an earlier record with a labelled clone, or
refill the first five slots from a deeper pool.

This contract is implemented by `evaluation/real_query_gate.py`,
`evaluation/semantic_recall.py`, `evaluation/original_query_recall.py`, and the
optional `evaluation/recall_stage_metrics.py` observer. It changes measurement,
not the recall result delivered by the engine. A regression test passing is
not evidence that production recall quality improved.

## Entry points and result channels

`run_production_recall_eval` dispatches `production_redacted_v1` datasets to the
real-query gate. Its scoring contract is `production_delivery_ranking.v1`.

`RecallBundle.items`, `rules`, and `reflections` are separate ordered channels.
Each frozen case may explicitly declare `evaluation_channel` as `items`,
`rules`, or `reflections`. An omitted channel means `items`, independently of
the labels. A rule label in a case without an explicit channel produces the
notice `rule_labels_require_explicit_channel_review`; the evaluator does not
silently switch to rules. An operator should review whether the intended
surface is the rule channel or a rule promoted into the items channel.

Every sample also reports `channel_rankings` for all three channels, preserving
their actual prefixes. These diagnostics do not change the chosen primary
channel. Switching the declared channel changes the frozen dataset digest.

The other recall scoring surfaces retain their declared roles:

| Entry point | Scored surface |
| --- | --- |
| General framework, memory benchmarks, ordinary production recall | The returned `bundle.items` order |
| Semantic acceptance | `bundle.items`, with explicit positive groups, negatives, forbidden refs, and path availability checks |
| Original-query rerun | Observed capture references and a separate engine rerun of `bundle.items`; neither overwrites the other |
| Explicit-query evaluation | Caller-observed explicit delivery, separately checked against authoritative native evidence |
| Controlled acceptance / semantic repair | Their existing fixed delivery/evidence assembly; labels do not choose its order |

`real_query_engine` is a facade over the same real-query scoring implementation.
CLI and evaluator-registry entry points therefore use the repaired calculation.

## Invocation depth and scoring depth

The production redacted gate currently calls recall with
`_REAL_QUERY_RECALL_DEPTH = 64` and scores the first **five actual slots** of
the declared channel. It does not deduplicate 64 results and then take five.
An answer at position 41 remains outside the scored prefix. Repeated records
occupy their original slots; a repeated relevant identity earns credit once.
Equivalence projection is only for matching labels to returned identities.

This is the prefix of that particular `limit=64` invocation. It is not a claim
that a separate online `limit=5` call, compact payload, or adapter injection
would return the same result. Candidate budgets and downstream selection may
depend on the requested limit. Semantic acceptance makes one `limit=5` call;
original-query reruns use the captured limit when it is available.

The real-query report includes an `evaluation_plan` derived from frozen cases:

- The frozen dataset digest.
- `invocation_depth: 64` and `scored_depth: 5`.
- The ordered case IDs and their selected evaluation channels.

`evaluation_plan_digest` is included in the logical report identity, persisted
attempt evidence, and baseline identity. Independent validation checks the
sample channels against this plan, verifies its digest and fixed depths, and
requires a matching baseline plan. Editing a sample channel, changing the plan
without updating its digest, or using a different baseline plan is rejected.

The existing native persisted payload and release lineage remain the authority
boundary. The plan is not a new cryptographic proof of physical delivery, nor
does it protect against an actor authorized to replace the entire report,
baseline lineage, and evaluator. The original dataset digest formula is
unchanged.

## Metric definitions

The existing gate thresholds are unchanged. The corrected delivery order must
be measured against a fresh compatible baseline; changing the arithmetic does
not justify lowering a threshold or declaring an improvement.

| Metric surface | Definition |
| --- | --- |
| Real-query gate `precision_at_5` | Distinct relevant ranking identities in the actual first five slots divided by `min(5, exact-scope active corpus capacity)`; this existing capacity normalization is explicitly reported. |
| `channel_rankings.*.precision_at_5` | Relevant identities in that channel's actual first five slots divided by **5**. It can differ from the gate metric for a small corpus. |
| Semantic / original-query P@5 | Relevant distinct record references in the actual first five slots divided by **5**. |
| P@1 | Whether the first actual slot is relevant. Semantic aggregate P@1 uses independently labelled positive cases. |
| MRR | Reciprocal of the first relevant **actual slot**, truncated at five; duplicate slots are not removed first. |
| Returned precision | Relevant distinct identities or references divided by actual returned slots, capped at five. Empty denominators are unknown. |
| Semantic group recall | Fraction of independently declared facts covered; references within a group are alternatives for one fact. |
| Semantic no-answer false recall | Fraction of independently labelled negative cases with returned items, among cases whose semantic path was verified. Missing or entirely unavailable negative coverage is unknown. |

Semantic `hit_at_1` and `hit_at_5` retain the existing additional requirement
that every declared fact group is covered within the first five slots. These
group-qualified gates are distinct from P@1 and MRR. A partial multi-fact answer
can have P@1 = 1 while failing the group-qualified acceptance gate.

Unavailable or degraded semantic paths cannot pass a case, even if an empty
result resembles a correct negative. Reports include `unavailable_count`,
`availability_rate`, latency P95, and the existing ordinary/assisted latency
contract. Unavailable negative cases have `false_recall: null` and are excluded
from the false-recall-rate denominator. Reports distinguish total labelled
`negative_count`, `negative_evaluated_count`, `negative_unavailable_count`, and
`negative_evaluation_coverage`. An entirely unavailable negative population has
an unknown false-recall rate, not a measured zero. The release gate still
requires zero unavailable cases. Older aggregates lacking the new delivery metrics retain unknown values;
the evaluator does not reconstruct them from a group-qualified hit boolean.

## Candidate Recall@K and observed stage loss

Semantic datasets can opt into a read-only observer:

```json
{
  "schema": "semantic_recall_cases.v1",
  "candidate_diagnostics": {"k": 64},
  "cases": [
    {
      "case_id": "independently-labelled-original-query",
      "query": "<original query supplied by the case owner>",
      "split": "holdout",
      "intent_group": "<stable independently assigned intent group>",
      "scope": {"user_id": "<authorized owner>"},
      "source_id": "<authorized source>",
      "task_context": {"target_source_id": "<the same authorized source>"},
      "expected_groups": [["<independently verified native record ID>"]]
    }
  ]
}
```

This is a format template, not a labelled dataset or acceptance evidence. Use
the actual captured context; do not change retrieval inputs just to make a
diagnostic bind successfully.

The observer reuses `retrieval.target_trace` around the **original single
invocation**. The target never becomes a candidate input. It neither reruns the
query nor trusts an explanation claiming to contain a target trace.

The existing trace binds one exact target, scope, source, engine and query.
Candidate Recall@K is known only for a single independently labelled target
when the native trace completed and its bounded candidate window is complete:

- A target absent from the complete observed candidate window is a miss.
- A target present in a complete pool of at most K observed entries is a hit.
- A target present in a larger pool has unknown Recall@K because the existing
  observer does not expose a global rank across providers.

Multiple label references, including equivalent alternatives, remain unknown
under this single-target observer. Binding mismatch, incomplete observations,
and non-native adapters also remain unknown. Negative cases have no positive
candidate target and are not applicable. The report includes known and unknown
positive counts, coverage, K, and an explicit denominator. It never converts
unknown observations to zero or stitches separate reruns into one candidate
pool.

`first_loss` means the first **observed adjacent** transition from target
presence to complete observed absence. It is not proof that no loss happened
inside an unobserved interval. Drop reasons are the native observer's bounded
reasons, not causes inferred from aggregate counts. These diagnostics explain
candidate availability and observed loss; they do not certify answer support.

## Independent holdout and shadow comparison

Use the existing `development`, `holdout`, and `regression` splits. Keep intent
groups in one split and keep the same original query plus exact authority
namespace in one split. The validator rejects cross-split reuse of these
identities. Rewording a case ID is not an independent holdout. Human review or a
separately controlled label source must establish the original labels, factual
alternatives, negatives and forbidden references before tuning begins.

The existing semantic release gate requires at least 60 cases, at least 20
positive and 20 negative holdout cases, and passing regression coverage. Its
minimums and quality thresholds are unchanged. This patch does not supply real
holdout data, independent labels, or a measured quality improvement.

[Explicit offline reranker comparison](reranker-shadow-evaluation.md) is a
separate entry point with bounded authorized candidates and an explicitly
injected scorer. Online recall does not invoke it. Its shadow scores must not
replace native delivery metrics or become semantic-confirmation evidence.
Compare native and shadow results on the same frozen independently labelled
cases; report failed/unlabelled comparisons and all attempted latency samples.

## Baseline migration

Reports predating `production_delivery_ranking.v1` may have used label-selected
rule heads or deduplicated deep pools. They remain historical evidence under
their original recorded arithmetic. They cannot be directly compared with the
corrected delivery metrics or renamed into the new contract.

The gate returns `baseline_scoring_contract_mismatch` for an incompatible
scoring contract and `baseline_evaluation_plan_mismatch` for a different plan;
the comparison status is explicitly `not_comparable`. Establish a new baseline
through the existing authorized release/bootstrap workflow, using the same
frozen cases and declared channels under the corrected evaluator. Preserve the
previous report and its measurements. A change caused only by scoring-contract
migration must not be reported as a retrieval improvement.
