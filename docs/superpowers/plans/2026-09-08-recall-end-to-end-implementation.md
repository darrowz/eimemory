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
  verification. Checked production configuration files expose no configured
  EIMEMORY LLM command; an abstract callback alone is not production integration.
- Complete full shadow generation, revision freshness and activation manifest.
- Bind mandatory original/negative/observed-ranking companion reports to the
  deployment receipt and independently verify them in the production gate.
- Obtain genuine Codex natural evidence; do not relabel acceptance-only cases.
- Pass known original regressions before consuming untouched holdout, then run
  one consolidated validation and only then merge/deploy/verify and notify.

The current implementation is partial. Existing production flags and release
remain unchanged; no claim of recall quality closure or L5 is made.
