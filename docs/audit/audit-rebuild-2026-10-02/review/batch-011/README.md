# Independent review: batch 011

Approved for publication of the bounded diagnostic-value propagation in `review-verdict.json`. Baseline: **afcc318483aa511251a8404c08faea4c34779608**.

The same twelve frozen pinned AST-shell methods produce seven failing methods/five passes before the patch (19 failure records including subtests) and twelve passes afterward. The prior 63 cases pass unchanged; diff-check exits 0. Across approved batches there are **75 distinct cases**: 35 ordinary fake unittest methods and 40 explicitly invoked AST-shell cases.

Only `_batch` changes, proved by independent whole-module AST restoration. The wrapper preserves the existing deadline mode and exactly six already-recognized incomplete reasons. Positive integer counts remain unchanged; booleans and other truthy values become integer 1, falsey values are omitted, and the wrapper's own budget signal uses a non-additive minimum of 1. Unknown fields, strings/container contents, status/error text and scoring-timeout reasons do not propagate. Complete local fallback and opaque hits retain their behavior. At most ten wrapper keys plus a synthetic timing key stay within the existing twelve-key budget.

The runner extracts only `_batch` into a base-free shell with inert batch/request/config/state metadata and a numeric lag stub. It uses raw output capture and exact-integer/recursive unknown-sentinel assertions, rather than relying on a real constructor to truncate or normalize output. No actual downstream incomplete consumer, selector, evidence, identity, admission, search, SQL, driver, model or network path executes. This verifies value propagation, not downstream decisions. The scoring-timeout policy and batch 008 remain on hold. The full suite remains unrun; remote publication is verified separately.

## Durable publication

The exact approved source and runner/test files were published and read back byte-for-byte at [01b43926](https://github.com/darrowz/eimemory/commit/01b439269ad5f9179584574beaa8d735e2b02785). The complete normalized before/after/regression and scope logs are embedded in [review-verdict.json](review-verdict.json), along with original and published-text hashes. Original logs remain unchanged in the verification record. No full-suite, production, or CI pass is claimed.
