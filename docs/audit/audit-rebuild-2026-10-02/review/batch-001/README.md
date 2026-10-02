# Independent review: batch 001

Approved for publication of the exact two-file patch recorded in `review-verdict.json`. Baseline: 4763001d1c4f3f4af6e6dda17e008e1b4c9b5609.

The reviewer applied the same test file to a separate fixed-base checkout. Baseline: 2 failures and 4 passes, exit 1. With the two production guards: 6 passes, exit 0. `git diff --check`: exit 0. The immutable publication-file bytes match the independently tested files exactly. Repair-owner logs were verified by hash and were not modified.

The failure oracle is preserved caller transaction state and pending writes, with no SQL, callback, commit, or rollback on rejected nested entry. Positive tests still verify own-transaction commit and rollback after callback failure for both APIs. No assertions were weakened.

The four-line production delta changes nested rejection to explicit RuntimeError and preserves caller work; normal callback, commit, outbox, and post-commit projection ordering is unchanged. Other transaction wrappers and BaseException handling are outside this repair.

Scope: fake-only standard-library unittest, importing RuntimeStore but bypassing its constructor. No real SQLite connection, model, network service, SQL credentials, production state, full suite, or deployment was exercised. Separate independent logs contain the exact commands and process exit codes.

The publisher subsequently verified remote commit [d1d571e109c99eab6540fd41f8d167e2502f012a](https://github.com/darrowz/eimemory/commit/d1d571e109c99eab6540fd41f8d167e2502f012a). This does not expand the local fake-only test scope or imply deployment.
