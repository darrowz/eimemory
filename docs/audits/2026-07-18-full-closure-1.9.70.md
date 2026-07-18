# EIMemory 1.9.70 candidate closure audit

## Scope

This audit covers the complete change set from `origin/master` to the 1.9.70
release candidate. It includes release identity, RPC authentication, OpenClaw
2026.7.1 compatibility, source provenance and intake safety, replay manifests,
L5 evidence, Feishu delivery, SQLite/runtime storage, and immutable deployment
rollback.

## Independent tools

- `code-review-graph` 2.3.6 indexed 492 files into 6,174 nodes and 66,583
  edges. Its change analysis covered 121 files and identified the highest risk
  score as 0.95.
- `open-code-review` 1.7.7 reviewed 103 files with MiniMax-M3 and produced 114
  comments: 2 critical, 15 high, 53 medium, and 44 low.
- The OpenCodeReview run completed with provider errors for
  `eimemory/storage/sqlite_store.py` and
  `tests/test_l5_closure_rehearsal.py`. Those files therefore require explicit
  local focused regression evidence before release instead of being counted as
  independently reviewed.

## Confirmed findings closed

- HTTP errors can no longer enter full-text parsing, and IPv4-mapped,
  IPv4-compatible, 6to4, and Teredo IPv6 forms cannot bypass the SSRF gate.
- RPC secret provisioning validates through a no-follow descriptor, rejects
  broad modes and hard links, and creates without overwriting a racing target.
- OpenClaw configuration updates are locked, bounded, inode-checked, durable,
  and create the required plugin allow policy.
- Immutable deployment detects dangling `current`, validates the rollback
  runtime, reports rollback failures truthfully, and preserves the failed
  release for diagnosis.
- Prompt-safety commands, provider responses, prompts, and package hashing have
  hard memory bounds. Optional LLM configuration failures fall back
  deterministically; safety and release gates remain fail-closed.
- Replay manifests use schema v2, explicit scope, transactional sequences, an
  allowlisted release identity, and release-bound evidence.
- Feishu v1 state migrates explicitly, ambiguous receipts never resend, terminal
  history is bounded, stale status-only workflows escalate, and the E2E tool is
  disabled unless explicitly enabled.
- SQLite rebuild keeps the exclusive temporary file, validates projections with
  static SQL, restores the live store after failure, and preserves durable
  export/recovery behavior.

## Findings rejected after inspection

- The reported missing deployment test helpers exist in the audited test file.
- The reported empty-release behavior in `live_task_acceptance.py` was attributed
  to code not present at the referenced locations; release-bound gates already
  fail closed when identity is absent.
- Duplicated replay fields in record content and metadata are intentional:
  content is evidence payload while metadata is the query/index projection.
- Corrupt Feishu delivery state intentionally stops processing; sending without
  trustworthy idempotency state would create duplicate messages.
- URL sources without a registry-bound identity intentionally remain untrusted.

## Release rule

The candidate may be versioned only after all focused layers pass, the two
provider-failed files receive compensating tests, the refreshed graph reports no
uncovered release-critical change, and the one final full suite either passes or
every reported failure is closed by a focused regression without repeating the
full suite.

## Final suite disposition

The single final full-suite run executed 1,851 tests: 1,828 passed, 19 were
platform skips, and four failed. One failure was a stale assertion that still
expected the pre-rollback `$RELEASE_DIR` literal instead of the generalized
`$target_release`. The other three required the optional, untracked 277 MB
LongMemEval dataset. The assertion was corrected; clean-checkout tests now mark
only those three real-data cases as explicit optional-data skips, while all
synthetic converter coverage remains mandatory. The three cases were also run
against the locally available real dataset and passed 3/3. The failed set was
then re-run as focused tests and closed without repeating the full suite.
