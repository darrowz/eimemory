# Independent review: batch 012

Approved for publication of non-finite timeout-input fallback, conditional on the publisher's current-source hash check in `review-verdict.json`. The independent fixed baseline is **afcc318483aa511251a8404c08faea4c34779608**; only `core/budgets.py` and its standalone test file are included.

The same eight frozen AST-shell methods yield two failing methods/six passes before the patch (18 subtest failure records), then eight passes. Four already-published pure AST shells also pass: 28 regression methods. Thus **36 cases pass at this checkpoint**. The 35 older runtime tests were intentionally not rerun because they can call a real environment-based accessor; independent batch 011 is not present in this fixed checkout.

Across the separate approved checkpoints, the cumulative inventory is **83 distinct cases** (35 ordinary fake unittest methods and 48 AST-shell cases). This is not an 83-test combined run on this tree.

Independent whole-module AST restoration proves that only two finite-number predicates and the required standard-library import changed. NaN, positive infinity and numeric-text overflow take existing fallback paths. Missing/blank/invalid/nonpositive input, legitimate finite overrides, defaults, caps, floors, margin arithmetic and other policy expressions are unchanged. The explicit-timeout documentation/policy question remains held.

Only `_positive_float` and `adapter_timeout_seconds` execute from extracted source; the margin is a literal source constant, `os.environ` is an inert synthetic mapping and the recall-budget dependency is a numeric stub. No actual process environment is read, copied, patched or changed; no project import, other budget helper, lock, transport, model or backend executes. The full suite remains unrun.

Because the independent pair was prepared in parallel with 011, the publisher must verify that current `core/budgets.py` still has SHA-256 **293a40ff623a97508caaf7751aca13857b094c49f56f43e43c879589cb842329**, then publish only the exact reviewed pair. No claim of remote publication is made here.

## Durable publication

The exact approved source and runner/test files were published and read back byte-for-byte at [f99fd2dd](https://github.com/darrowz/eimemory/commit/f99fd2dd89fb6a3fc48fe2e9519f9cf7180b1d1d). The complete normalized before/after/regression and scope logs are embedded in [review-verdict.json](review-verdict.json), along with original and published-text hashes. Original logs remain unchanged in the verification record. No full-suite, production, or CI pass is claimed.
