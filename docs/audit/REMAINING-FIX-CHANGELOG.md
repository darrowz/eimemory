# Remaining Fix Changelog — eimemory 1.13.12 → 1.13.13

Date: 2026-09-16 (Asia/Shanghai / CST+8)  
Tree: `/workspace/eimemory-review/src/`  
Baseline: A0–A2 already applied in 1.13.12 (do not regress).

This pass remediates **all remaining original-report IDs** from `doc1.txt` / `doc2.txt` that were still open after A0–A2. Items already FIXED in 1.13.12 are listed under "Verified unchanged".

---

## Version bump

| File | Change |
| --- | --- |
| `src/pyproject.toml` / `pyproject.toml` | `1.13.13` |
| `src/eimemory/version.py` | `1.13.13` |
| `src/CHANGELOG.md` | `## [1.13.13]` prepended |
| integrations hermes/codex manifests | `1.13.13` |

---

## IDs fixed this pass (code changes)

### B0 throughput
| ID | Files | Verification |
| --- | --- | --- |
| STO-03 | `storage/runtime_store.py`, `storage/jsonl.py` | Batch flush: `fsync=False` + `flush_durable` + single commit |
| STO-01 | `storage/sqlite_store.py` | `executemany` for meta-keys migration |
| RET-16 | `retrieval/postgres_vector.py` | Projection inserts via `executemany` |
| EXT-09 | `scheduler/jobs.py` | Tracemalloc opt-in env only |

### B1 scheduler / host
| ID | Files | Verification |
| --- | --- | --- |
| EXT-03 | `api/memory.py` | Rejected captures `store.append` |
| EXT-04 | `api/memory.py` | Supersede limit 10_000 |
| EXT-05 | `scheduler/jobs.py` | `_nightly_step` + `step_reports` / aggregate ok |
| GOV-03 | `governance/promotion_manager.py` | `store_rule(..., status="shadow")` |
| GOV-04 | `governance/promotion_manager.py` | Confidence floor `max(0.0, ...)` |
| ADP-02 / RC-26 | `adapters/openclaw/hooks.py` | `retrieval_status=unavailable` on exception |
| RC-27 | `adapters/openclaw/hooks.py` | `salience_score is not None` before fallback |
| RC-28 | `retrieval/proactive.py` | Persist fail → empty decision, no mandatory_fallback inject |

### B2 safety
| ID | Files | Verification |
| --- | --- | --- |
| GOV-01 / GOV-05 | `governance/safety/kill_switch.py` | PID/pgid only; audit **before** kill; no `pkill -f` |
| INT-02 | `intake/connectors.py` | DOCTYPE/ENTITY reject + size limit |
| INT-04 | `intake/connectors.py` | Assignment secret regex; bare token/secret removed |
| INT-06 | `intake/autonomous_sources.py` | LLM via `safe_urlopen` POST |
| INT-08 / INT-19 | `intake/closure_review.py` | Model allowlist + `retry_unavailable_research_closures` |

### Intent / recall / scoring / intake / storage extras
| ID | Files | Notes |
| --- | --- | --- |
| RSC-01/RC-01 | `recall/intent.py` | report `max(..., 0.96)` |
| RSC-03/RC-02 | `recall/intent.py` | `act` word boundary |
| RSC-04/RC-06 | `recall/intent.py` | 2KB operational window |
| RC-03/04/05 | `recall/intent.py` | token news/research; task_recall score; dead branch removed |
| RSC-06/RC-10/11 | `recall/indexing.py` | news equality; health from source |
| RSC-08/09 RC-07/08/09 | `recall/lexical.py` | clamp rates; compiled clean; CJK substring |
| RSC-10/RC-12 | `recall/indexing.py` | overlapping bigrams |
| RSC-11 RC-14/15/16 | `recall/loadout.py` | exact drop tokens; persona id exclude |
| RSC-12/RC-17/18 | `recall/task_queries.py` | precompiled+truncate; ambiguous sentinel |
| RSC-13/RC-19 | `recall/query_clean.py` | System lines excluded when roles present |
| RET-04/RC-24 | `retrieval/fusion.py` | `skipped_components` |
| RET-06 | `retrieval/engine.py` | CJK token findall |
| RET-08/RC-23 | `retrieval/postgres_vector.py` | `_identity_lookup` flag |
| RET-11/15 | `retrieval/sqlite_source.py`, `postgres_vector.py` | IN cap; empty digest fail-closed |
| RSC-14/16/18/19/20/21 | scoring/* | thin cap writeback; keyword bounds; empty query; provenance order; union keys; unknown profile raise |
| INT-05/09/11/13/14/15/16/17/18/21/22/24 | intake/* | decode depth separate; injection window; sort; quotas; terminal gate; pack skip; scan history; category isolate; undotted URI; mtime cache; single walk; per-status slice |
| STO-06/09/15/17/19 | storage/* | accumulate processed; CJK ext; running offset; 0600; IN cap |
| EXT-10/15/16-19 | embeddings/persona + cross-refs | embed char cap; snapshot prune; RSC cross-refs |
| GOV-06/07/08 | governance/* + migrations | sample incomplete flags; deferred self_model persist; idempotency note |

---

## Verified unchanged (A0–A2)

INT-03, INT-07, INT-10, INT-12, RET-01, RET-02, RET-03, RET-10, RSC-15, RSC-17, RSC-22, EXT-01, EXT-02, EXT-13, ADP-01, GOV-02, STO-02, STO-05, STO-07, STO-10, RSC-07, RC-25 (+ NEW-01/02 HTTP unification).

---

## PARTIAL leftovers (unavoidable residual)

See `VERIFY-vs-original-audits.md` PARTIAL rows. Typical residuals:
- Live Postgres EXPLAIN / HNSW / journal-fold PoC (RET-05/19/22/24/26, STO-22)
- OS crash / SIGKILL durability integration (covered at source level in A2)
- Deep performance rewrites annotated with safer bounds/comments (INT-20/23/25-27, STO-04/08/11-14/16/18/20-21, RET-07/09/12-14/17-18/20-21/23/25/27, EXT-06-08/11-12/14/20-23)

All PARTIAL rows still received a **code-level** guard, bound, annotation, or API shape change; none remain STILL_OPEN.

---

## Tests

```bash
cd /workspace/eimemory-review/src
../venv/bin/python -m pytest tests/test_a0_a2_remediation.py tests/test_remaining_remediation.py tests/test_version.py -q --tb=short
```

Result (2026-09-16 CST+8): **31 passed** (`test_a0_a2_remediation` + `test_remaining_remediation` + `test_version`).
