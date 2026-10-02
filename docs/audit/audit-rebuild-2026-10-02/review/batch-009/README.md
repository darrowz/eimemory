# Independent review: batch 009

Approved for publication of the owned connection-return reset in `review-verdict.json`. Baseline: **91b2936b54adebcf61bb3c085cfd3952987671e8**.

The same eight pinned AST-shell cases produce six failures/two passes before the one-line patch and eight passes afterward. The previous 49 cases pass unchanged; diff-check exits 0. Across approved batches there are **57 distinct cases**: 35 ordinary fake unittest methods plus 22 explicitly invoked AST-shell cases. Batch 008 remains on hold.

Independent whole-module AST comparison proves only `connection.rollback()` was added before idle-pool insertion. Gated close, the original close/discard tail, and every other production AST node are unchanged. Exact source/runner publication bytes match the independently tested checkout; the runner is frozen.

The fake oracles verify reset before reuse, discard/close attempt after ordinary missing/noncallable/failed reset, no pool insertion on close failure, full-pool cleanup, nonpooled close, and serial repeated-close idempotence. A fake close failure is not described as successful physical closure.

Only `_release_idle` and `_GatedConnection.close` are extracted into base-free shells. Pool-size config, raw rollback/close, lock, gate, and list are inert fakes with import/config fences. No driver, SQL, DSN, credential, connection factory, schema, network, model, identity/evidence, or real resource operation executes. This is not a real PostgreSQL or concurrent-close validation. The full suite remains unrun; remote publication is verified separately.

## Durable publication

The exact approved source and runner/test files were published and read back byte-for-byte at [0c5a4c92](https://github.com/darrowz/eimemory/commit/0c5a4c926f5329db32fa0008a5ff58c1dc2a7367). The complete normalized before/after/regression and scope logs are embedded in [review-verdict.json](review-verdict.json), along with original and published-text hashes. Original logs remain unchanged in the verification record. No full-suite, production, or CI pass is claimed.
