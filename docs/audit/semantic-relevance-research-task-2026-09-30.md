# Semantic relevance for Hermes `research.task` recall, per channel scope (1.14.17)

Date: 2026-09-30
Approved step after 1.14.16: let the semantic relevance monitor judge Hermes
`research.task` recall decisions, and run it in every exact channel scope, so that
new Hermes recalls can obtain the semantic signal (S_sem) that
`production-recall-auto-review.v1` requires.

## What changed

- `semantic_relevance_monitor.ELIGIBLE_TASK_TYPES = ('memory.recall', 'research.task')`.
  `research.task` is the task type Hermes' provider uses for its automatic per-turn
  proactive recall (`adapter.proactive_prefetch`). Both surfaces deliver retrieved
  memory for one user query, so the same prompt, parser, fail-closed verifier and
  cache apply unchanged:
  - the decision must be release-bound to a verified deployment receipt, not in the
    control cohort, not acceptance-generated, and single-source;
  - the query must come from the private vault (or a hash-bound legacy copy) and match
    `query_digest`;
  - the delivered items must re-hydrate with the exact delivered render digest;
  - malformed or inconsistent provider output is `unknown`.
  Other task types are still skipped.
- Provenance: new observations record `decision_surface` (the decision's task type)
  and `channel`, stored in the exact channel scope (for example
  `embodied::channel::hermes`). `memory.recall` observations written before 1.14.17
  without these fields are still reused. For `research.task`, the provenance is
  mandatory.
- `monitor_channel_deliveries` runs the monitor in each exact channel scope
  (`openclaw`, `codex`, `hermes`) derived from the base scope. Previously the nightly
  passed only the base scope, so Hermes decisions (stored in `…::channel::hermes`)
  were never judged. Each channel keeps the existing 8-new-calls-per-run budget, and
  results are cached per evaluation identity.
- The new nightly step `semantic_relevance_monitor` runs before
  `production_recall_auto_review` and `production_recall`, so tonight's observations
  are usable by tonight's auto-review. `quality_gap_intake` uses the same per-channel
  function and reuses the cache; off-topic findings from every channel still become
  recall quality gaps.
- Auto-review accepts a semantic record only when its `decision_surface` equals the
  decision's (eligible) task type and its `channel` equals the exact scope's channel.
- Nightly diagnostics: `recall_semantic_relevance` reports new, reused, deferred,
  provider calls, verdicts, `by_surface` and `by_channel` (bounded counts only).

Unchanged: the 15-label threshold, criteria v1 (S_sem AND (verified proof OR host
`used`)), release authority and scope rules. The judge is still observation-only.

## Delivery and `used` feedback on Hermes (the remaining gap)

The code path exists:
`prefetch` → `adapter.proactive_prefetch` (research.task, returns `pm:` citations)
→ the `eimemory-hook` `pre_llm_call` hook → `adapter.proactive_ack` (items become
`injected`) → `post_llm_call` → `adapter.proactive_terminal` with
`used_citations` = the `pm:` citations the assistant actually cited.

On honrui, however (read-only probe, 2026-09-30), none of the 44 persisted
decision items across 184 Hermes decisions was ever acknowledged
(`ever_injected=0`), including the `context_delivered` decisions on 1.14.13/1.14.14.
`eimemory-hook` is enabled and its hooks register. The ack requires the
`pre_llm_call` user message to match the prefetch query for the same session, and it
is skipped when no `pm:` citation was returned. We did not wire around this: treating
"returned to the host" as "delivered" would be dishonest. Until acks arrive, the
monitor records `empty_delivery` (unknown) for these decisions, and auto-review keeps
them pending (`no_candidate_delivered`).
