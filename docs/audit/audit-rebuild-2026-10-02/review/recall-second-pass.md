# Independent recall boundary source review

Baseline: **4763001d1c4f3f4af6e6dda17e008e1b4c9b5609**, version 1.14.31.

## Result

The source confirms A-STO-003: a deadline can cut off candidate collection before any rows are collected, yet the search returns an ordinary empty success. This is independent source corroboration of a candidate supplied by the first reviewer, not a blind review or a runtime reproduction.

Exact reviewed scope: 81 additional function definitions, documented with source hashes and exact ranges in `recall-second-pass-coverage.json`:

- `storage/sqlite_store.py` 4164–4248, 4250–5232, and 6763–6781: 34 functions
- `retrieval/sqlite_source.py` full 1–376: 16 functions, including three nested callbacks
- `retrieval/contracts.py` full 1–272: 18 functions, including protocol methods

- `retrieval/diagnostics.py` full 1–197: 9 functions
- `retrieval/stage_diagnostics.py` full 1–101: 4 functions, including three nested helpers

This adds four whole small modules and another partial range of the large SQLite module. It does not mark the whole storage or retrieval package reviewed.

## A-STO-003: empty candidate cutoff loses the timeout signal

1. `_candidate_rows` (4631–4767) performs setup and partition checks before collector calls
2. At 4677, 4687, 4698, 4706, and 4714, an expired collection deadline prevents further collectors from running
3. If no keys were collected, 4733–4738 returns only zero candidate count, limit, and empty candidate sources
4. `search_with_diagnostics` (4382–4629) checks its scoring deadline inside the row loop. With zero rows, that check never executes
5. The final report can therefore retain `recall_index_hybrid` with no timeout count, and RuntimeStore's `_search_result` classifies it as non-degraded

This is distinct from a valid empty exact partition or a query that exhausts its complete candidate search while the budget is still live. The proposed failure oracle should advance a fake clock after successful setup, require the deadline cutoff to remain observable at the RuntimeStore boundary, and verify that genuine empty results remain non-degraded. It must not load an actual embedding model; patch that dependency to a fake if the public scoring method is exercised.

## Boundary preservation requirements

- Preserve exact tenant/agent/workspace/user/source reference verification. `SQLiteCandidateSource` explicitly supplies `_exact_scope=True`, and verifies identity candidates again against authoritative records
- Preserve bounded hit and diagnostic contracts. Candidate limits, freezing, and source provenance are behavior-bearing, not formatting
- Maintain both `search` and `search_with_diagnostics` behavior. A collector exception can be translated by RuntimeStore; a partial-result report needs its timeout signal preserved through `_batch`
- `SQLiteCandidateSource` relabels reports with identity results to `identity_hybrid` (264–266). A timeout represented solely by retrieval-mode text could be overwritten; the blocked-count signal matters too
- `_batch` keeps only eight alphabetically sorted blocked reasons (347–348). A new timeout reason should be checked for survival if it becomes the sole indicator of incomplete work
- The current scoring deadline check can return partial scored rows. Do not replace that separate established path with unconditional empty results merely to fix this empty-collection case

## What this review did not establish

No runtime import, model call, SQLite connection, concurrency test, real SQL execution, production request, or whole-suite test was run for this second source pass. SQL construction and scope filtering were inspected, but that is not proof of execution plans, query performance, isolation under contention, or semantic retrieval quality. The static import graph was used for navigation only.

The extra 20 functions cover tokenizer/schema helper context and diagnostic report shaping. They do not prove that a schema migration or the downstream evidence consumer is correct. Imports from diagnostic modules into evidence-related helpers were not followed or executed. `compact_recall_diagnostics` explicitly retains recall_budget_exhausted and candidate_scoring_timeout engine drops; stage diagnostics carries bounded drop dictionaries.
