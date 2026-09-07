# Semantic recall closure implementation

## Execution contract

User-authorized implementation, 2026-09-08. Complete code first, use only minimal
checks during implementation, commit coherent changes promptly, then run exactly
one full suite. Repair failures with targeted regressions, not another full run.
Merge master and deploy only after the relevant release gates pass. Deliver the
final result to the verified Feishu private chat; delivery is not completion.

## Baseline

Production 1.11.83 / 183983099fd0b8d051e45c53968b1600acae7133; PostgreSQL
candidate path disabled. 1839 active-memory vectors built in 4086 seconds.
Prior explicit acceptance: positives 6/6 at five, 5/6 at one; negatives 0/2.
Natural coverage last recorded 10/15, Codex 0/5, not a quality pass.

## Work packages

- [ ] Preserve and commit existing source-faithful import, capture, hash/lexical,
      PostgreSQL projection/reuse and deployment work.
- [ ] Bounded independent reranker client and single post-fusion admission gate;
      fail closed on unavailable/malformed scoring, authorize before scoring and
      revalidate content before returning. Explicit identity lookup stays distinct.
- [ ] Pin and provision a loopback-only resource-limited reranker; no OpenClaw
      migration, upstream modifications or external memory inference.
- [ ] Durable incremental projection maintenance including deletion, alias and
      scope changes, concurrent writes, resumes and exact compatibility binding.
- [ ] Original-query/capture/negative-evidence contract and independent quality
      metrics; synthetic checks remain separate from natural production evidence.
- [ ] Complete tests, deployment documentation and version-bound report artifacts.
- [ ] One Linux full-suite run; only targeted regressions after failures.
- [ ] Real-corpus, resource and end-to-end deployment acceptance; same-candidate
      production report and strict state before claiming full closure.

## Fixed gates

Known eight cases: six equivalent-answer groups first, two unrelated queries
empty, no labeled unrelated tails. Independent positive hit@1 >= .90, no-answer
false recall <= .05, precision of returned items >= .90, zero forbidden/deleted
hits, full recall p95 <= 3000 ms; retain stricter existing gates. Never loosen a
gate or rewrite a question to fit observed results. Freeze development/holdout
sets by intent/session; at least 60 explicit cases, independently report genuine
natural samples (three channels, at least five per channel). Missing real usage
is an evidence dependency, never fabricate it or mark acceptance-only as natural.

## Rollback

Retain SQLite authority and committed PG index. Disable optional candidate and
reranker services/configuration together when necessary, restoring a verified
conservative retrieval release rather than accepting weak hash-only evidence.
Retain diagnostic artifacts. Confirm health and identities after any rollback.
