# Independent verification checkpoints

Fixed source baseline: 4763001d1c4f3f4af6e6dda17e008e1b4c9b5609, version 1.14.31. Baseline source coverage and behavior verification of patches are separate measures.

## Published source repairs

- [001: transaction ownership](batch-001/README.md)
- [002: reader-acquisition deadlines](batch-002/README.md)
- [003: empty candidate cutoff](batch-003/README.md)
- [004: callback-abort cleanup](batch-004/README.md)
- [005: exact-reference chunk bounds](batch-005/README.md)
- [006: export wrapper ownership](batch-006/README.md)
- [007: empty-timestamp cursor continuation](batch-007/README.md)
- [009: owned connection reset before reuse](batch-009/README.md)

008 remains on hold for concurrency ownership; the missing number is intentional. 010 has no published repair in this checkpoint.

After 009 there are 57 distinct passing fake cases: 35 ordinary unittest methods plus 22 explicit source/hash-pinned AST shell cases. Existing regressions are counted once. Standalone shells are not ordinary pytest discovery. No real driver, SQL, PostgreSQL/SQLite integration, production, full-suite, or CI pass is claimed.

Each repair verdict records its exact source/test hashes and verified remote commit. For 003 onward, complete normalized logs are embedded in the verdict JSON with both original and published-text hashes; no separate log file is required.

## Source coverage

[The complete original coverage manifest](../coverage-manifest.json) currently preserves 323 unique paired baseline function definitions with exact file hashes, ranges, graph IDs, and independent evidence pointers. Independent review included 328 unique functions; the extra context is not added to the paired count. This is not a completed repository audit. Supplementary reports are saved in bounded batches as they are verified.
