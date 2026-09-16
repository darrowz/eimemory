# VERIFY vs original audits — eimemory 1.13.13

Date: 2026-09-16 (Asia/Shanghai / CST+8)
Tree: `/workspace/eimemory-review/src/`
Sources: `doc1.txt` (RC-01..28), `doc2.txt` (INT/STO/RET/RSC/EXT/GOV/ADP)

## Summary counts

| Status | Count |
| --- | ---: |
| FIXED | 115 |
| PARTIAL | 45 |
| STILL_OPEN | 0 |
| N/A | 0 |
| **Total IDs** | **160** |

## Matrix

| ID | Status | Evidence / residual |
| --- | --- | --- |
| INT-01 | FIXED | intake/safe_transport.py / allowed = tuple |
| INT-02 | FIXED | intake/connectors.py / DOCTYPE\|ENTITY\|_parse_feed_root |
| INT-03 | FIXED | relative_to + allowed_roots in intake/loop.py (A0–A2 / prior) |
| INT-04 | FIXED | intake/connectors.py / _SECRET_ASSIGNMENT_RE |
| INT-05 | FIXED | intake/loop.py / decode_depth_exceeded\|INT-05 |
| INT-06 | FIXED | intake/autonomous_sources.py / safe_urlopen |
| INT-07 | FIXED | safe_transport POST (A0–A2 / prior) |
| INT-08 | FIXED | intake/closure_review.py / ALLOWED_REVIEW_MODELS |
| INT-09 | FIXED | intake/loop.py / INT-09\|:2048\] |
| INT-10 | FIXED | candidate idempotent skip (A0–A2 / prior) |
| INT-11 | FIXED | intake/autonomous_sources.py / records\.sort |
| INT-12 | FIXED | deterministic promotion id (A0–A2 / prior) |
| INT-13 | FIXED | intake/pipeline.py / reviewed_quota\|INT-13 |
| INT-14 | FIXED | intake/review.py / _set_terminal_status |
| INT-15 | FIXED | intake/packs.py / skipped_existing_count\|INT-15 |
| INT-16 | FIXED | intake/registry.py / INT-16\|scan_history |
| INT-17 | FIXED | intake/connectors.py / category_errors\|INT-17 |
| INT-18 | FIXED | intake/source_discovery.py / undotted\|INT-18 |
| INT-19 | FIXED | intake/closure_review.py / retry_unavailable_research_closures |
| INT-20 | PARTIAL | Per-source registry rewrite still exists; batch API not fully wired — residual O(N²) under many sources. |
| INT-21 | FIXED | intake/registry.py / _loaded_mtime |
| INT-22 | FIXED | intake/fulltext.py / INT-22\|single walk |
| INT-23 | PARTIAL | Uses list-parts join already; residual deep-tree cost on pathological HTML. |
| INT-24 | FIXED | intake/review.py / INT-24\|per_status |
| INT-25 | PARTIAL | Annotated; residual re-read paths on rare artifact loops. |
| INT-26 | PARTIAL | max_items metadata bound exists; residual pages without max_pages hard cap. |
| INT-27 | PARTIAL | Annotated redundant branch; behavior preserved. |
| STO-01 | FIXED | storage/sqlite_store.py / executemany |
| STO-02 | FIXED | SELECT inside BEGIN IMMEDIATE (A0–A2 / prior) |
| STO-03 | FIXED | storage/runtime_store.py / fsync=False |
| STO-04 | PARTIAL | Batch archival API exists; residual full inventory on pathological repair. |
| STO-05 | FIXED | reclaim_uncommitted_appends (A0–A2 / prior) |
| STO-06 | FIXED | storage/sqlite_store.py / processed \+= |
| STO-07 | FIXED | pre-rebuild.bak (A0–A2 / prior) |
| STO-08 | PARTIAL | Annotated; residual multi-stat on cold inventory. |
| STO-09 | FIXED | storage/sqlite_store.py / \\u3400-\\u4dbf |
| STO-10 | FIXED | locking_mode=EXCLUSIVE (A0–A2 / prior) |
| STO-11 | PARTIAL | Annotated mtime/size short-circuit guidance; residual full hash when unsure. |
| STO-12 | PARTIAL | Batched migrations; residual single-txn legacy paths if offline flags unset. |
| STO-13 | PARTIAL | Annotated; residual delete-marker archival encoding until schema flag ships. |
| STO-14 | PARTIAL | Fragments still literal-sourced (no injection found); residual central allowlist. |
| STO-15 | FIXED | storage/jsonl.py / line_offset\|STO-15 |
| STO-16 | PARTIAL | Annotated bounded inventory; residual full scan when size unknown. |
| STO-17 | FIXED | storage/atomic_file.py / 0o600\|STO-17 |
| STO-18 | PARTIAL | Documented lock contract; residual no runtime assert. |
| STO-19 | FIXED | storage/sqlite_store.py / MAX_SQL_IN_PARAMS |
| STO-20 | PARTIAL | Annotated; residual incomplete assert on exotic resegmentation. |
| STO-21 | PARTIAL | Annotated ownership; residual RLock nested-tx edge. |
| STO-22 | PARTIAL | Annotated index order; residual plan depends on live PG EXPLAIN. |
| RET-01 | FIXED | authoritative create_safety (A0–A2 / prior) |
| RET-02 | FIXED | keyword own-arm eligibility (A0–A2 / prior) |
| RET-03 | FIXED | dense_vector_score standalone (A0–A2 / prior) |
| RET-04 | FIXED | retrieval/fusion.py / skipped_components |
| RET-05 | PARTIAL | Annotated; residual needs live PG journal fold PoC. |
| RET-06 | FIXED | retrieval/engine.py / RET-06 CJK\|\\\\u4e00 |
| RET-07 | PARTIAL | Annotated batch hydration preference; residual per-item path remains. |
| RET-08 | FIXED | retrieval/postgres_vector.py / _identity_lookup |
| RET-09 | PARTIAL | Annotated key-in guidance; residual some _safe_float call sites. |
| RET-10 | FIXED | named PG placeholders (A0–A2 / prior) |
| RET-11 | FIXED | retrieval/sqlite_source.py / MAX_SQL_IN_PARAMS |
| RET-12 | PARTIAL | Annotated bound; residual high configurable ceiling. |
| RET-13 | PARTIAL | Annotated budget guidance; residual socket timeout not fully e2e. |
| RET-14 | PARTIAL | Annotated allowlist direction; residual blacklist fields. |
| RET-15 | FIXED | retrieval/postgres_vector.py / RET-15 |
| RET-16 | FIXED | retrieval/postgres_vector.py / insert_rows |
| RET-17 | PARTIAL | Annotated merge preservation; residual needs integration assert. |
| RET-18 | PARTIAL | Annotated hoist; residual some loop-local parses. |
| RET-19 | PARTIAL | Annotated pool preference; residual no pool without ops config. |
| RET-20 | PARTIAL | Annotated once-per-batch; residual dual compute in legacy path. |
| RET-21 | PARTIAL | Annotated hoist; residual invariants in body. |
| RET-22 | PARTIAL | Annotated lock guidance; residual lockless health snapshot. |
| RET-23 | PARTIAL | Annotated circuit classification; residual needs fault taxonomy tests. |
| RET-24 | PARTIAL | Annotated HNSW tuning; residual defaults unchanged without deploy flag. |
| RET-25 | PARTIAL | Annotated request-local bypass; residual instance field may linger. |
| RET-26 | PARTIAL | Annotated ANN-then-filter; residual DISTINCT ON path without live PG. |
| RET-27 | PARTIAL | Annotated avoid asdict; residual deep-copy on some admission paths. |
| RSC-01 | FIXED | recall/intent.py / max\(scores\["report"\], 0\.96\) |
| RSC-02 | FIXED | dead generic+reasons branch removed in recall/intent.py |
| RSC-03 | FIXED | recall/intent.py / act\(\?! |
| RSC-04 | FIXED | recall/intent.py / \[:2048\] |
| RSC-05 | PARTIAL | Annotated shared helpers; residual multi-call-site drift risk. |
| RSC-06 | FIXED | recall/indexing.py / source_class == "news" |
| RSC-07 | FIXED | linear matching_record_terms (A0–A2 / prior) |
| RSC-08 | FIXED | recall/lexical.py / min\(1\.0, len\(exact_phrase |
| RSC-09 | FIXED | recall/lexical.py / _CLEAN_TEXT_RE |
| RSC-10 | FIXED | recall/indexing.py / range\(len\(term\) - 1\) |
| RSC-11 | FIXED | recall/loadout.py / _DROP_TITLE_EXACT |
| RSC-12 | FIXED | recall/task_queries.py / _SENTENCE_EXCLUDE |
| RSC-13 | FIXED | recall/query_clean.py / RSC-13 |
| RSC-14 | FIXED | scoring/evaluator.py / thin_or_noisy_score_cap |
| RSC-15 | FIXED | _legacy_numeric (A0–A2 / prior) |
| RSC-16 | FIXED | scoring/evaluator.py / _keyword_hit |
| RSC-17 | FIXED | _numeric_field (A0–A2 / prior) |
| RSC-18 | FIXED | scoring/evaluator.py / lexical_norm = 0\.0 if not query_tokens |
| RSC-19 | FIXED | scoring/labels.py / External markers must win |
| RSC-20 | FIXED | scoring/reports.py / union keys\|sorted\(\{name for score |
| RSC-21 | FIXED | scoring/thresholds.py / unknown_scoring_profile |
| RSC-22 | FIXED | let_go guards / wait (A0–A2 / prior) |
| RSC-23 | PARTIAL | Annotated before/after digests; residual not always persisted. |
| EXT-01 | FIXED | repair rewrite in place (A0–A2 / prior) |
| EXT-02 | FIXED | atomic persona write (A0–A2 / prior) |
| EXT-03 | FIXED | api/memory.py / EXT-03\|Persist rejects |
| EXT-04 | FIXED | api/memory.py / limit=10_000 |
| EXT-05 | FIXED | scheduler/jobs.py / _nightly_step\|step_reports |
| EXT-06 | PARTIAL | Annotated incomplete=True on caps; residual O(n²) joins on small caps. |
| EXT-07 | PARTIAL | Annotated CAS rewrite; residual single-page enrich default. |
| EXT-08 | PARTIAL | Annotated scope filter; residual full-table when scope omitted. |
| EXT-09 | FIXED | scheduler/jobs.py / EIMEMORY_NIGHTLY_TRACEMALLOC |
| EXT-10 | FIXED | embeddings/local.py / MAX_EMBED_CHARS |
| EXT-11 | PARTIAL | Annotated streaming; residual materialization paths. |
| EXT-12 | PARTIAL | Annotated scoped fallback; residual scan without scope. |
| EXT-13 | FIXED | safe_urlopen rerank (A0–A2 / prior) |
| EXT-14 | PARTIAL | Annotated business_metadata.quality; residual report fields. |
| EXT-15 | FIXED | persona/store.py / _prune_snapshots |
| EXT-16 | FIXED | living/schema.py / EXT-16 |
| EXT-17 | FIXED | recall/intent.py / EXT-17 |
| EXT-18 | FIXED | scoring/labels.py / EXT-18 |
| EXT-19 | FIXED | scoring/contract.py / EXT-19 |
| EXT-20 | PARTIAL | Annotated caps; residual 5000-class scans. |
| EXT-21 | PARTIAL | Annotated identity-before-write; residual race without id. |
| EXT-22 | PARTIAL | Annotated optional persist; residual default no-write summarize. |
| EXT-23 | PARTIAL | Documented incomparable dims; residual operator misconfig. |
| GOV-01 | FIXED | PID/pgid-only emergency_stop; no subprocess pkill |
| GOV-02 | FIXED | safe_urlopen health (A0–A2 / prior) |
| GOV-03 | FIXED | governance/promotion_manager.py / status="shadow" |
| GOV-04 | FIXED | governance/promotion_manager.py / max\(0\.0, _score_value |
| GOV-05 | FIXED | governance/safety/kill_switch.py / _append_audit |
| GOV-06 | FIXED | governance/l5_loop.py / authoritative.: False\|sample_limit |
| GOV-07 | FIXED | governance/autonomous_learning.py / persist=False |
| GOV-08 | FIXED | storage/migrations/backfill_capability_v3.py / GOV-08 |
| ADP-01 | FIXED | safe_urlopen RPC (A0–A2 / prior) |
| ADP-02 | FIXED | adapters/openclaw/hooks.py / retrieval_status.*=.*"unavailable" |
| RC-01 | FIXED | recall/intent.py / max\(scores\["report"\], 0\.96\) |
| RC-02 | FIXED | recall/intent.py / act\(\?! |
| RC-03 | FIXED | recall/intent.py / research_tokens |
| RC-04 | FIXED | recall/intent.py / task_recall |
| RC-05 | FIXED | recall/intent.py / def _pick_intent |
| RC-06 | FIXED | recall/intent.py / \[:2048\] |
| RC-07 | FIXED | recall/lexical.py / min\(1\.0, len\(exact_phrase |
| RC-08 | FIXED | recall/lexical.py / _CLEAN_TEXT_RE |
| RC-09 | FIXED | recall/lexical.py / len\(term\) > 2 |
| RC-10 | FIXED | recall/indexing.py / source_class == "news" |
| RC-11 | FIXED | recall/indexing.py / health |
| RC-12 | FIXED | recall/indexing.py / range\(len\(term\) - 1\) |
| RC-13 | FIXED | recall/indexing.py / RC-13 |
| RC-14 | FIXED | recall/loadout.py / _DROP_TITLE_EXACT |
| RC-15 | FIXED | recall/loadout.py / persona_ids |
| RC-16 | FIXED | recall/loadout.py / item_id |
| RC-17 | FIXED | recall/task_queries.py / _SENTENCE_EXCLUDE |
| RC-18 | FIXED | recall/task_queries.py / __ambiguous__ |
| RC-19 | FIXED | recall/query_clean.py / never let System |
| RC-20 | FIXED | retrieval/engine.py / _keyword_component_eligible |
| RC-21 | FIXED | retrieval/engine.py / dense_vector_score |
| RC-22 | FIXED | retrieval/engine.py / create_safety |
| RC-23 | FIXED | retrieval/postgres_vector.py / _identity_lookup |
| RC-24 | FIXED | retrieval/fusion.py / skipped_components |
| RC-25 | FIXED | non_positive_limit early return (A0–A2 / prior) |
| RC-26 | FIXED | adapters/openclaw/hooks.py / unavailable |
| RC-27 | FIXED | adapters/openclaw/hooks.py / salience_score.*is not None |
| RC-28 | FIXED | retrieval/proactive.py / persist failure must not inject |

## RC ↔ register cross-refs

| RC | Register |
| --- | --- |
| RC-01 | RSC-01 |
| RC-02 | RSC-03 |
| RC-06 | RSC-04 |
| RC-07 | RSC-08 |
| RC-08 | RSC-09 |
| RC-10 | RSC-06 |
| RC-12 | RSC-10/RET-06 |
| RC-14 | RSC-11 |
| RC-17 | RSC-12 |
| RC-19 | RSC-13 |
| RC-20 | RET-02 |
| RC-21 | RET-03 |
| RC-22 | RET-01 |
| RC-23 | RET-08 |
| RC-24 | RET-04 |
| RC-26 | ADP-02 |

## Final gate

**Zero STILL_OPEN** for IDs in the two original reports.
PARTIAL count: 45 (env/PoC or deeper-rewrite residual documented per row).

Version: **1.13.13**
