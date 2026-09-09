# Recall health and acceptance implementation plan

**Goal:** Separate request cancellation from backend health, retain bounded diagnostics on native RPC responses, then commit, deploy and verify the repaired release.

**Architecture:** Preserve request-local admission and exact authority/index checks. Backend availability requires a successful query against the still-verified identity; request-budget cancellation does not revoke that proof, while real failure or identity change does. Publish the last request outcome separately. Timings are request-local, bounded, numeric and allowlisted, never query text, credentials or raw exceptions.

**Constraints:** Default 3-second/10-second budgets and admission thresholds remain unchanged. No fabricated task truth, host attestations, natural labels, lineage evidence or L5 qualification. The user's continuation and explicit commit/deploy/acceptance request authorize execution of the preceding diagnosis. Work on a dedicated branch in the clean checkout; use one implementing agent. Historical task evidence is not an authoritative current-state materializer.

## 1. Health contract

Completed locally: failing regressions followed by green health/adjacent tests and independent review. Last-query fields refer to the last PostgreSQL attempt, not an empty SQLite-only shortcut.

- [ ] Add tests in `tests/test_postgres_vector_source.py`: successful query then budget cancellation preserves backend availability but query validity is false; no prior proof remains unavailable; genuine failures and watermark changes invalidate proof; RPC health exposes only safe independent fields.
- [ ] Run those tests and observe failures on c6d2e9d.
- [ ] Update `eimemory/retrieval/postgres_vector.py` to store the successful backend query identity separately; set it only on completed search, clear it on real failures and incompatible refresh, leave admission identity unchanged. Update `eimemory/adapters/eibrain/rpc_server.py` to expose `index_verified`, `query_valid`, and `last_query_status` alongside availability.
- [ ] Run health and request-budget regressions.

## 2. Request diagnostics and evidence boundaries

Completed locally: instrumented snapshot reproduced 10/12 evidence_found, and localized lexical/ngram/hint-freezing optimizations produced 12/12 on the same snapshot. Empty hydration-interrupted searches now report unavailable. Consolidated 479 focused tests and 6 packaging tests passed. See the audit document for exact evidence and limitations.

- [ ] Add failing tests for per-source stage timings on success and timeout, bounded engine aggregation, and compact/native output retention. Test unknown diagnostic keys cannot leak.
- [ ] Instrument source index/embedding wait/local/search/recheck phases with request-local counters; attach numeric timings and stable stage labels to each batch. Do not join expired embedding workers or alter the cache's scope/authority identity.
- [ ] Add bounded phase summaries to engine diagnostics and compact bundle output, plus explicit historical-only task evidence semantics. Verify no impact on evidence signatures or authority gates.
- [ ] Use the isolated production-data snapshot and production dependencies for a cold/warm replay; save failures and identify dominant phases before choosing any performance change. Only optimize a demonstrated in-scope cause with a failing regression first.

## 3. Release and acceptance

- [ ] Run focused adjacent tests and review the diff. Align Python/package/Hermes versions for 1.13.7 if the release is still 1.13.6; do not overwrite concurrent work.
- [ ] Commit and integrate the verified branch. Use the existing immutable deployment workflow, dependency preservation and rollback safeguards. Preserve original failed acceptance artifacts.
- [ ] Verify serving commit/import root, RPC/storage health, cold and warm four-query results, request/backend state separation, and actual host receipt evidence where available. Keep historical task status, formal closure and L5 acceptance separate.
- [ ] Record results and remaining evidence gaps; do not promote a lightweight release or direct RPC result into formal closure or host attestation.
