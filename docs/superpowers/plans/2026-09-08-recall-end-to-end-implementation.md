# Recall end-to-end closure implementation

Approved scope: repair the complete chain without a new resident model or a
SQLite vector experiment. Preserve original queries, authority boundaries and
quality gates. Baseline release: 1.12.0 / 6d109fa0.

## Delivery checklist

- [ ] P0 shared label authority, historical observation lifecycle, bounded repair
  pagination and immutable dataset invalidation/publication.
- [ ] P1 shared query identity, real proactive-to-vault replay, private bounded
  collection and all-channel collection diagnostics.
- [ ] P2 bounded caller-assisted original-query retrieval and source verification.
- [ ] P3 independent maintenance/serving controls, managed worker, full shadow
  generation, incremental freshness and activation manifest.
- [ ] P4 label-independent observed ranking, release-bound original and negative
  evaluation, explicit unavailable accounting.
- [ ] P5 one consolidated validation, real-channel evidence, release/activation
  and independent verification. Do not manufacture Codex natural samples.

Commit coherent batches promptly. Run minimal failing/repaired checks while
implementing; do not repeatedly run the full suite. Run the untouched holdout
only after known regressions pass. All incomplete items remain explicit.

## Implementation checkpoint (2026-09-08)

Feature branch: `codex/recall-end-to-end-closure`. No production activation.

Implemented and committed through `8ae72a6f`:

- Shared task-prefixed query identity and real proactive-to-private-vault test.
- Historical empty/missed retrieval observations remain eligible; removed gold
  invalidates its labels and dependent cases, not the authentic observation.
- Shared positive label validation at acceptance, repair and dataset hydration;
  validate the whole operator packet before writing any labels.
- Live authority manifests at dataset staging, activation and evaluation.
- Complete repair scans paginate with bounded snapshots (10,000 rows per type
  safety cap); bootstrap uses complete scanning rather than a 500-row prefix.
- Maintenance/serving controls separated, worker lifecycle installed by release
  tooling, admission deadline checked on all paths including exact-ID hits.
- Replay reports distinguish unavailable and incomplete context; negative replay
  checks exact scope/source and reuses verified original context when available.

Verification: Linux candidate `702fbc34` passed 61 related tests. Subsequent
  operator packet changes passed 5 focused tests locally; gold lifecycle changes
  passed 10 locally. Candidate `8ae72a6f` then passed all 29 Linux label/dataset/
  repair tests. These are not a consolidated full-suite acceptance result.

Still required before calling the plan complete:

- Wire caller-assisted retrieval into actual clients, with bounded original-query
  verification. The earlier file-only configuration check was incomplete: the
  live RPC process does have an existing OpenClaw model command (see below).
- Complete full shadow generation, revision freshness and activation manifest.
- Bind mandatory original/negative/observed-ranking companion reports to the
  deployment receipt and independently verify them in the production gate.
- Obtain genuine Codex natural evidence; do not relabel acceptance-only cases.
- Pass known original regressions before consuming untouched holdout, then run
  one consolidated validation and only then merge/deploy/verify and notify.

The current implementation is partial. Existing production flags and release
remain unchanged; no claim of recall quality closure or L5 is made.

## Implementation checkpoint (2026-09-09)

- User approved ordinary recall at 3 seconds and difficult-query verification
  at no more than 10 seconds. Reports separate ordinary p95 and assisted maximum;
  unavailable responses do not count as successful abstentions.
- Actual isolated PostgreSQL reads were enabled and verified against all 330
  active memories in the target partition, not a gold-only corpus. Both original
  probes used current generation evidence without falling back to SQLite-only
  retrieval. This is technical activation proof, explicitly NOT a quality pass.
- The full shadow table `closure_candidates_20260908` is building from production
  SQLite authority with resumable, target-namespaced snapshots. Build remains in
  progress; production reads have not yet been activated.
- Existing xai/grok-4.6 can select the correct original-question source quote.
  End-to-end known-regression verification still times out near 10 seconds and
  therefore fails. No holdout has been consumed. SDK cold import measured 3.3 s;
  an optional request-scoped prewarm overlaps this with retrieval, caps concurrent
  preparers at two, and kills unused processes. No resident model was added.
- Gateway model-only calls now use supported `low` thinking, explicit model
  identity validation, model-only sessions and a server-side timeout
  derived from the caller deadline. Timeouts must not leave long model runs.
  Reserved internal-session flags are rejected by the official external client
  and are not used. The model-only command is not natural acceptance evidence.
- Shared recall configuration reaches RPC, OpenClaw and maintenance worker.
  Client transport margins cover the approved 10-second engine budget. The
  example file is not an activation command and keeps serving flags disabled.
- Original/negative/observed-ranking companion reports are release-bound and
  independently checked; this code does not create missing natural samples.
- Focused command/prewarm tests: 14 passed. Consolidated suite, quality gates,
  final merge/deploy, runtime client checks and Feishu delivery remain pending.
