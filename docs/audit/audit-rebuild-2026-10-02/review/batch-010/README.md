# Independent review: batch 010

Approved for publication of the owned connection-permit cleanup in `review-verdict.json`. Baseline: **0c5a4c926f5329db32fa0008a5ff58c1dc2a7367**.

The same six frozen pinned AST-shell cases produce one failure/five passes before the one-handler patch and six passes afterward. The prior 57 cases pass unchanged; diff-check exits 0. Across approved batches there are **63 distinct cases**: 35 ordinary fake unittest methods plus 28 explicitly invoked AST-shell cases. Batch 008 remains on hold.

Independent whole-module AST comparison proves that only the outer `_connect` cleanup handler changes from `Exception` to `BaseException`. The selected invocation acquires its permit before the try block and releases it on failure before transfer; the tests preserve existing permits and the raised exception object. Failed acquisition and pre-acquisition abort release nothing; successful idle handoff does not release early.

Only `_connect` is extracted into a base-free shell. Gate, lock, idle object and numeric timeout calculations are fake. The abort happens before driver imports, factory calls or DSN access, guarded by import/config fences. There is no actual database, network, credential, model, interrupt or resource operation. This does not establish every asynchronous-signal or constructor interruption case. The full suite remains unrun; remote publication is verified separately.

## Durable publication

The exact approved source and runner/test files were published and read back byte-for-byte at [afcc3184](https://github.com/darrowz/eimemory/commit/afcc318483aa511251a8404c08faea4c34779608). The complete normalized before/after/regression and scope logs are embedded in [review-verdict.json](review-verdict.json), along with original and published-text hashes. Original logs remain unchanged in the verification record. No full-suite, production, or CI pass is claimed.
