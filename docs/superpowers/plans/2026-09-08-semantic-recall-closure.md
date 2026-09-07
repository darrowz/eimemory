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

- [x] Preserve and commit existing source-faithful import, capture, hash/lexical,
      PostgreSQL projection/reuse and deployment work.
- [x] Bounded independent reranker client and single post-fusion admission gate;
      fail closed on unavailable/malformed scoring, authorize before scoring and
      revalidate content before returning. Explicit identity lookup stays distinct.
- [ ] Pin and provision a loopback-only resource-limited reranker; no OpenClaw
      migration, upstream modifications or external memory inference.
- [x] Durable incremental projection maintenance including deletion, alias and
      scope changes, concurrent writes, resumes and exact compatibility binding.
- [x] Original-query/capture/negative-evidence contract and independent quality
      metrics; synthetic checks remain separate from natural production evidence.
- [ ] Complete tests, deployment documentation and version-bound report artifacts.
- [x] One Linux full-suite run; only targeted regressions after failures.
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

## Implementation checkpoint, 2026-09-08 04:10 Asia/Shanghai

Not a release acceptance or a production cutover. Production still runs 1.11.83.

- The sole full Linux suite ran at `010fad440646292e6fbc7f5d4b915dbaaa5e6510`:
  3,864 passed, 5 failed, 7 skipped, 2,492 seconds. Subsequent validation was
  targeted only. Fixed the Codex package version, incomplete export from a journal
  tail, and authorized exact-match ordering; installed the missing optional PDF
  parser in the validation environment. All identified failures are covered by
  passing focused corrections; the original expensive ingestion fixture was not
  rebuilt. A fast 10,005-record paginated export regression passed.
- Codex hooks and explicit MCP recall now have an adequate semantic transport
  budget, preserving short non-recall hooks. Proactive default source now matches
  the `codex` partition. honxin still needs its installed plugin cache upgraded
  from 1.11.70 and the legacy 2-second transport override removed at cutover.
- A real, isolated PostgreSQL acceptance verified bootstrap, changed-only vector
  update, deletion without embedding, stale CAS rejection, and preservation of
  the committed generation. Its temporary schema was removed, not production data.
- Production's derived index snapshot resumed using existing vectors and caught
  up to 1,840 memories/revision 2 in about 85 seconds over two bounded calls.
  Watermark: `sync-ed0e11f851a340749553d29d8a21e723`.
- Reranker revision `2cfc18c9415c912f9d8155881c133215df768a70` is loaded on
  127.0.0.1:8089. Its original 2 GiB cap caused OOM; 3 GiB allowed initialization,
  and observed idle memory was about 1.65 GiB. No automatic restart during
  validation. Production inference flags remain disabled.
- Development calibration is running, with no holdout inference yet. The private
  68-case packet is frozen (20 development, 40 holdout, 8 regressions), SHA-256
  `e49e2dd871652b605121e4c04f9719c33f14f2da1294c4f8110297f9199bb5f7`.
- The nightly maintenance timer/service were temporarily stopped for the release
  window; restore their prior active state before handoff. Recovery metadata is
  in the validation checkout's private `.tmp/semantic-maintenance.json`. OpenClaw
  was not stopped or modified. The separate Codex heartbeat `honxin` remains paused.

Pending: frozen quality/resource acceptance, final branch merge and push, immutable
deployment, native plugin upgrade, production report/strict state/closure checks,
maintenance restoration, and final verified Feishu notification. None is implied
by the passing unit tests or inference health.
