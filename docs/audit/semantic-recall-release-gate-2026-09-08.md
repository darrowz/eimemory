# Semantic recall implementation and release gate, 2026-09-08

## Outcome

Implementation is committed on `codex/semantic-recall-closure`. The candidate is
**not admitted for master merge or production cutover**. Production remains
1.11.83 / `183983099fd0b8d051e45c53968b1600acae7133`.

The user-requested single full Linux suite completed: 3,864 passed, 5 failed,
7 skipped in 2,492 seconds. All five identified failures were subsequently
resolved with focused checks only; there was no second full suite. Corrections
covered the native Codex manifest, optional PDF test dependency, journal-tail
export completeness, and exact-identity ranking without losing other grounded
results. The 10,005-record export regression uses paginated records instead of
repeating the expensive ingestion fixture. Two added artifact/deployment checks
also passed.

The actual PostgreSQL backend passed isolated bootstrap, changed-only update,
deletion, and stale-generation CAS checks. Production's derived index caught up
to 1,840 memories / authority revision 2 in about 85 seconds by reusing compatible
vectors. SQLite remains authoritative. No local SQLite vector experiment or
upstream OpenClaw changes were introduced.

## Quality and resource evidence

Pinned reranker: `BAAI/bge-reranker-base`, revision
`2cfc18c9415c912f9d8155881c133215df768a70`.

- FP32 initialization exceeded a 2 GiB cap. At 3 GiB it loaded, with approximately
  1.65 GiB steady memory. Development quality alone passed at a calibrated
  threshold, but the best full recall p95 was approximately 3.19 seconds (>3 s).
- Official ONNX Runtime 1.29.0 dynamic INT8 packaging of MatMul/Gather produced a
  279,301,884-byte model. First bounded build hit its 3 GiB RAM + 2 GiB swap cap;
  the build succeeded with RAM unchanged and temporary swap allowance of 4 GiB.
  Inference allowed no additional swap, and used approximately 700 MiB.
- Derived artifact manifest SHA-256:
  `65c79417e664c589d4a10d29cabd5e12f0f6644d37d04dcbb2821d2f8db71634`.
  The model directory binds this digest, all files are checked, and deployment
  mounts it read-only. The original stopped container/configuration is retained.
- INT8 development-only configuration (6 candidates, 128 chars) reached hit@1
  90%, negative false recall 0%, returned precision 100%, p95 2.42 seconds. This
  was **not** a release pass: known eight-case regression then passed only one
  of six positives, while both negatives correctly returned empty.
- Expanding authoritative text to 320/512 chars restored strong Fujian scores,
  revealing harmful 128-character truncation. However, WeChat paraphrases still
  ranked too low; one was below an unrelated candidate. No fixed threshold met
  both development negatives and all known regressions. Full-text development
  p95 remained about 2.07/2.30 seconds, so quality, not merely speed, blocks release.
- A focused FP32 comparison on the same known candidate texts also showed the
  paraphrase ranking failure. Reverting quantization alone does not solve it.

Frozen packet: 20 development, 8 known regression, 40 holdout cases; SHA-256
`e49e2dd871652b605121e4c04f9719c33f14f2da1294c4f8110297f9199bb5f7`.
**Holdout inference was never run**, because the known regressions failed first.
No labels, original questions, thresholds for success, or natural sample counts
were rewritten to manufacture a pass.

## Remaining release blockers

The present reranker plus absolute-logit admission is insufficient for the
required conversational paraphrases. A stronger semantic verifier or a revised
admission model needs its own resource and frozen quality acceptance; lowering
the score threshold is not a justified correction. This is a design/quality
blocker, not a claim that PostgreSQL is unavailable.

Natural evidence is independently incomplete: Hermes 5/5, OpenClaw 5/5, Codex
0/5. Native Codex plugin cache upgrade, legacy 2-second override removal, and
private original-query capture remain pending the approved release cutover.
Synthetic/explicit cases cannot substitute for genuine Codex usage. A current
production recall report and strict state are not established by this candidate.

Deployment has therefore been withheld. Stop optional unadmitted rerankers and
restore the previously active nightly service/timer before handoff. Preserve
the private calibration reports, full-suite XML, focused correction logs and
maintenance recovery record under the isolated validation checkout's `.tmp`.
Feishu notification must state non-completion rather than imply deployment.
