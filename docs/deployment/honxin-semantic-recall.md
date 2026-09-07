# honxin semantic recall rollout

This rollout keeps SQLite authoritative. PostgreSQL is a rebuildable candidate
index, and embedding runs in a separate localhost-only CPU container. OpenClaw
is not modified or migrated.

## Provisioned embedding service

- Container: `eimemory-embeddings`, restart policy `unless-stopped`.
- Image: `ghcr.io/huggingface/text-embeddings-inference@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07`.
- Model: `BAAI/bge-base-zh-v1.5`, revision `f03589ceff5aac7111bd60cfc7d497ca17ecac65`, 768 dimensions.
- Binding: `127.0.0.1:8088:80`; model cache `/var/lib/eimemory-embeddings`.
- Limits: 1 CPU, 1536 MiB RAM with no additional swap, 128 PIDs, one tokenizer worker,
  512 batch tokens, 8 concurrent requests, client batches up to 8.
- Container UID/GID match cache owner, `no-new-privileges`, all capabilities dropped.
- Private configuration: `/etc/eimemory/embedding-service.env` (0600). Do not
  print raw startup logs: TEI can log its API key. Read logs through redaction.
- Client configuration: `/etc/eimemory/embedding.env` (0600), OpenAI-compatible
  base URL `http://127.0.0.1:8088/v1`, client batch 4, timeout 15 seconds,
  projection text bound 1600 characters. The server truncates beyond its token limit.

## PostgreSQL and projection domain

Dedicated PostgreSQL database/role `eimemory_vector`, loopback only; credentials
are in `/etc/eimemory/postgres.env` (0600). Do not use the existing Twenty database.
Set `EIMEMORY_POSTGRES_VECTOR_DIMENSION=768` and
`EIMEMORY_POSTGRES_PROJECTION_MEMORY_ONLY=1` for this staged rollout.

The optional domain indexes **active memory records only**. Other record types
remain searchable through SQLite. A separate derived revision fence covers all
memory mutations, including deletion, archival, kind transitions and alias edits.
Governance/audit writes alone do not invalidate this domain. This does not relax
per-hit SQLite revalidation, scope isolation, or snapshot consistency.

Unchanged vectors can be reused only from a committed watermark with matching
embedding/projection fingerprints and an exact projection digest. Changed records
must be embedded again; model or projection changes require rebuilding. When
changing the model revision, also change the configured embedding fingerprint
(`EIMEMORY_EMBEDDINGS_FINGERPRINT`) to bind the new model and pooling contract.

## Deployment and verification

1. Run focused Linux tests and `git diff --check`; do not interpret Windows POSIX
   permission-test failures as production permission passes.
2. Migrate the candidate schema explicitly and build the index with the same
   model, projection domain and text bound that RPC will use.
3. Run acceptance queries against the complete authoritative corpus, checking
   both relevant answers and unrelated questions. Require actual PostgreSQL
   availability, not silent SQLite fallback.
4. Release from `/dev-project/eimemory` with the full commit and
   `EIMEMORY_INSTALL_POSTGRES_EXTRA=1`. Optional runtime configuration files are
   loaded by the versioned RPC service. Keep `EIMEMORY_POSTGRES_VECTOR_ENABLED=0`
   until the index and candidate recall verification pass.
5. Install the optional `eimemory-vector-sync.service` and `.timer` as user units
   and enable the timer after cutover. It skips an already-current index and
   performs bounded resumable maintenance otherwise. Do not enable two sync workers.
6. Verify GitHub, remote HEAD, current symlink and `/health` identities agree.
   Check OpenClaw remains healthy, memory pressure and query latency under load,
   and safe fallback on embedding failure.
7. Publish explicit acceptance separately from natural production recall evidence.
   Imported memory and operator queries must not become natural benchmark cases.
   Missing natural samples, production reports or strict state remain failures.

Rollback: disable the optional sync timer and set the PostgreSQL enable flag to
zero, then restart RPC. SQLite data is untouched by disabling this optional path.
Keep derived PostgreSQL/cache data for investigation; do not delete authoritative data.

## In-progress checkpoint — 2026-09-08 00:35 Asia/Shanghai

Not a completed release. RPC remains 1.11.83 / `183983099fd0b8d051e45c53968b1600acae7133`;
the optional PostgreSQL flag is still disabled in production. No new commit/tag
has been published. PostgreSQL migration and the independent embedding endpoint
are verified, but full semantic recall acceptance is still pending.

- Latest focused Linux suite: **249 passed**; compileall and diff check passed.
- Embedding short-query smoke: 768 finite values, about 0.30 s; loaded model about
  461–467 MiB. Full-length indexing saturates its 1 CPU allocation.
- First complete build runs as user unit `eimemory-vector-bootstrap.service` from
  `/dev-project/eimemory-recall-11184-validation/.tmp/build_memory_vectors.py`.
  Inspect its journal before starting another worker. At the checkpoint it had
  reported 160 processed active memories; active memory count was 1839.
- Native Hermes source-faithful line 11 import was applied to production after
  checking digest `b619904bdc08830e6e9554cded161ae769b651328cd2156e4a240e88402e7757`.
  Record `mem_26a674a1141d0dfda1ec1dff26d9132c` is not a natural benchmark sample.
- Candidate SQLite-only baseline on the real partition: 5/8 explicit checks pass.
  Two low-overlap WeChat questions and the ice-cream unrelated-query rejection
  still fail. Use `.tmp/verify_production_semantic_recall.py --vector` after the
  index completes; do not present SQLite fallback as vector acceptance.
- Derived memory revision was stable across Runtime initialization and recall
  (`1 -> 1 -> 1`). The prior bootstrap interruption was the intentional native
  memory repair, not a reason to disable consistency checks.
- The bootstrap process started before the committed-vector reuse optimization;
  its current initial build is compatible. Newly staged code includes reuse and
  the bounded maintenance worker. Verify reuse against real PostgreSQL before
  enabling the maintenance timer.
- A current-task follow-up named `完成 honxin 语义召回部署并发飞书` was created
  with automation ID `honxin`, active every 10 minutes. It must continue the
  validation/release/notification work, and be paused after final delivery.
- User Feishu home routing exists in Hermes configuration. Read credentials only
  in memory and verify the destination/application pairing; do not pick arbitrarily
  between OpenClaw's two allowlisted users. Final notification has not been sent.

### Follow-up — 2026-09-08 00:59 Asia/Shanghai

- Bootstrap was still active and had reported 720 records by 00:55:44, with no
  authority revision restart. Embedding memory was about 486 MiB.
- Fixed missing bytecode protection on the new maintenance unit. The RPC auth
  file remains mandatory; its regression now explicitly permits only the two
  optional vector configuration files instead of rejecting every optional file.
- Linux deployment subset (`rpc or stage or immutable`): **42 passed** after
  those fixes. Initial archive-only validation checkout lacked Git metadata;
  initialized it at the original baseline commit without changing candidate files.
  No authoritative repository or running release was changed by this test setup.
- OpenClaw and RPC remained active; sampled memory pressure was zero and the
  two live vmstat intervals showed no swapping. Full recall acceptance remains pending.

### Acceptance checkpoint — 2026-09-08 01:55 Asia/Shanghai

- Bootstrap completed at 01:36:28: **1839 active memories**, 4086.1 seconds,
  committed watermark `sync-86c9b0cdbc3545ad8b28b2a5800e47c4`, revision `1`.
- Real-corpus PostgreSQL acceptance confirmed `available`, `index_verified=true`,
  `query_valid=true`, with no bypass. Six positive questions reached top five
  (five top one), but both unrelated-query rejections failed: **6/8, not passed**.
- A source-marked hash collision was improperly accepted as standalone grounding.
  New regression failed before the fix; SQLite now labels its hash score and the
  selector requires corroboration for that score. Linux adjacent suite: **135 passed**.
  This removes the hash-collision result but does not solve dense false positives.
- Testing the model's official query instruction also left the same acceptance
  failures. It was a diagnostic monkeypatch only and was not deployed/configured.
  Relevant and irrelevant dense scores overlap; do not select a threshold merely
  to fit these eight queries.
- Actual PostgreSQL reuse probe: four compatible vectors reused; wrong model and
  wrong projection digest reused zero. Probe did not mutate the committed index.
- User was asked whether to expand scope to an independent relevance reranker,
  with renewed resource/latency validation. No reranker was deployed. Automation
  `honxin` is paused pending that decision, not marked complete.
- Verification report sent to the configured Hermes Feishu home **private chat**;
  message ID `om_x100b66dbdacbdca4dd4fbb4d6fcdd89`, content readback verified.
  This proves API delivery, not that the user read it.
- No release commit/push/cutover performed. RPC stays 1.11.83, production vector
  flag stays disabled. SQLite, OpenClaw and existing databases remain in place.
