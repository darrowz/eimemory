# Lightweight recall implementation and acceptance, 2026-09-08

## Outcome

Code is committed on `codex/semantic-recall-closure`; **not admitted for master
merge or production inference cutover**. Fresh production health still reports
1.11.83 / `183983099fd0b8d051e45c53968b1600acae7133`. RPC, OpenClaw and the nightly
timer are active. No new reranker or model service was started.

Implemented:

- Source-bound extractive spans in PostgreSQL, linked by parent storage key,
  scope/source, parent digest, span offsets and generation watermark. Updates,
  deletes and old-generation retirement cascade to the derived spans.
- Shared Chinese/Unicode query/document tokenization and per-arm candidates.
- Dense leaders retain their reserved slot even when also found deep in the
  SQLite ordering. Integrity metadata precedes optional diagnostics under the
  32-field CandidateHit bound; otherwise correctly retrieved records lost their
  digest schema/timestamp and were rejected downstream.
- Independent lightweight admission, exact identity lookup, conservative
  duplicate suppression and final authority revalidation. Raw cosine and RRF
  are not answer probabilities. Explicit price questions require monetary
  evidence rather than a matching entity with unrelated numeric attributes.

The algorithms are independently implemented; upstream frameworks were not
imported wholesale. See the architecture note for reviewed upstream commits.

## Validation

The earlier single full Linux suite remains the broad baseline; it was **not
rerun**. This turn's initial related suite passed 208 tests, followed only by
focused checks for subsequent corrections. The latest lightweight unit suite
passed 21 tests. Real PostgreSQL lifecycle checks passed, covering bootstrap,
scope isolation, changed spans, delete cascade and stale-generation CAS. The
real lifecycle test is now checked into the repository with an explicit test
DSN requirement and UUID-named synthetic tables.

Naive per-sentence indexing would create 37,135 spans for 1,850 active memories.
Packing contiguous original sentences reduced this to 7,408. Full-corpus
candidate indexing was paused with resumable progress retained; the production
index was not replaced.

For bounded quality validation, an isolated, source-faithful copy contains all
330 active memories in the exact evaluated production scope/source partition,
not just the expected answers. Its PostgreSQL index completed in 1,135.54 s.
Watermark: `sync-e4955ee875fc42f487abaf8a56a5443a`. The copy is acceptance-only,
not natural evidence and not proof of whole-production deployment.

Development (20 frozen cases):

- Hit@1 and hit@5: 100% (10 positive queries).
- Negative false recall: 0% (10 negative queries).
- Returned precision: 100%; forbidden hits and unavailable calls: zero.
- Full-call p95: 816.124 ms.

Development calibration SHA-256:
`8ef002eb4bf0df62c8d07317c08518e2a72f0da2fa3513877c21a4886b789163`.
Chosen cosine minimum 0.5, lexical coverage minimum 0, lexical weight 0.1,
score gap 0, candidate bound 48. These are development-selected parameters,
not globally calibrated probabilities.

Known regression (8 frozen cases): **7/8 passed**. Five of six positives hit
first, both negatives returned empty, returned precision 100%, p95 748.297 ms.
`reg-w4`, the negated "web article / title only" phrasing, returned no evidence.
Its correct short memory scored 0.4203, below an unrelated candidate at 0.4776.
Lowering a threshold therefore cannot safely fix its ordering.

Regression artifact SHA-256:
`44740ddd638a760e2bcbd32195540fcbb603c5750cb1f608dabb57fe08088b3a`.
**The 40-case holdout was not run**, because the known regressions did not pass.
No original queries, expected groups, forbidden references or success gates
were changed. Failed intermediate reports are retained separately in the
validation checkout's private `.tmp` directory.

Read-only diagnosis of the remaining case tried the model's official retrieval
instruction, original question clauses, and the source user utterance. None
provided sufficient evidence of a safe fix. These diagnostics are not enabled
in the shipped code, and no alternate model was downloaded or activated.

## Remaining boundary

The approved lightweight work is implemented but insufficient for all frozen
queries. An agent-assisted clarification/reformulation stage would be a new
behavioral step that needs end-to-end validation; this report does not silently
substitute it for the failed original-query gate. Production recall receipts,
strict release state and genuine Codex natural samples remain separate from
these explicit tests. Do not merge/deploy this candidate as a completed recall
quality repair on the basis of development success alone.

Fresh authoritative-runtime formal checks still return
`release_identity_unavailable` and `strict_state_missing`; no release receipt
or strict pass was manufactured from the candidate checkout.
