# eimemory 1.13.11 复核审计报告（合并版）

| 项目 | 内容 |
| --- | --- |
| 复核日期 | 2026-09-16（Asia/Shanghai） |
| 基线版本 | eimemory **1.13.11**（`pyproject.toml` / `version.py` 一致） |
| 源码根 | `/workspace/eimemory-review/src/` |
| 权威前册 | `AUDIT-business-loop-2026-09-16.md` + `doc1.txt` / `doc2.txt` |
| 包规模 | 约 **369** 个 `.py` 文件，约 **168,365** LOC（全包）；主线 intake/storage/retrieval/recall/scoring/living ≈ **39,963** LOC |
| 方法 | 静态对照：按符号/行为定位当前行号；`rg`+Read；不实现产品修复 |
| 输出 | 本文件 + `REAUDIT-status.json` |

---

## 一、执行摘要

### 1.1 总体结论

对照当前拷贝到 box 的 **1.13.11** 源码，**前册正式 P0 无一被判定为 FIXED**。
登记册（不含 RC 交叉编号）共 **132** 项：
- **仍开放 STILL_OPEN**：130
- **部分修复 PARTIAL**：1（STO-10）
- **已修复 FIXED**：1（RSC-07）
- **未能核实 COULD_NOT_VERIFY**：0
- **RC-01..28**：全部复核；与 RET/RSC/ADP 交叉引用，**不重复计入独立缺陷头数**。
- **新发现 NEW**：5 项（同族 SSRF/urlopen 旁路与宿主 fail-open）。

**发布建议：仍不建议发版。** 13 个正式 P0 中 12 个 STILL_OPEN + STO-10 PARTIAL；INT-10/INT-12 候选 P0 仍开放。须完成 A0/A1/A2 列车并配回归后再评估。

### 1.2 仍开放按严重度（登记册，含 PARTIAL）

| 严重度 | 仍开放(+部分) |
| --- | --- |
| P0 | 15 |
| P1 | 53 |
| P2 | 53 |
| P3 | 10 |
| **合计** | **131** |

**正式 P0 仍开放/部分**：EXT-01, EXT-02, INT-03, INT-10, INT-12, RET-01, RET-02, RET-03, RET-10, RSC-15, RSC-22, STO-02, STO-05, STO-07, STO-10
**前册 P0 已 FIXED**：无

### 1.3 贯穿性判断（相对前册）

三条主线**未改写**：安全原语旁路、幂等/批次「看起来对」、字段存在性/or 短路判定。
登记册唯一明确 FIXED 项是 **RSC-07**（词法匹配已线性化）；召回专项 **RC-25** 亦 FIXED（`engine` 对 `limit<=0` 早退）。**STO-10** 因强制 `offline=True` 记 PARTIAL，但替换阶段仍释放 SQLite 独占连接。

---

## 二、版本与方法

1. `pyproject.toml` → `version = "1.13.11"`；`eimemory/version.py` → `__version__ = "1.13.11"`。
2. 完整阅读前册 AUDIT 与 doc1/doc2 登记；对每个 ID 用函数名/行为字符串在当前树定位（**不信任旧行号**）。
3. 主动扫描 `governance/`、`adapters/hermes/`、`cli/main.py`、`evaluation/`、`deploy/`：urlopen、路径、fail-open、or-0.0、非原子写、幂等缺口、create_safety、融合资格。
4. Cloud Agents 不可用；全部在本 box 完成。未做动态 PoC。

---

## 三、完整复核矩阵（零遗漏）

状态枚举：`STILL_OPEN` | `FIXED` | `PARTIAL` | `COULD_NOT_VERIFY`。路径相对 `eimemory/` 包。

### 3.1 intake

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| INT-01 | STILL_OPEN | P2 | `intake/safe_transport.py` | 204,205 | if any(_is_disallowed_address(address) for address in addresses): raise UnsafeURL(...) |
| INT-02 | STILL_OPEN | P1 | `intake/connectors.py` | 57,90 | root = ET.fromstring(xml_text) |
| INT-03 | STILL_OPEN | P0 | `intake/loop.py` | 417,436 | def _local_path_from_uri(uri: str) -> Path \| None:  # no relative_to(root) |
| INT-04 | STILL_OPEN | P1 | `intake/connectors.py` | 25,33 | SECRET_MARKERS includes bare "token","secret" |
| INT-05 | STILL_OPEN | P2 | `intake/loop.py` | 552,573 | decode overflow treated as containing secret；pattern confirmed via injection/secret markers in loop.py |
| INT-06 | STILL_OPEN | P1 | `intake/autonomous_sources.py` | 515,524 | with urlopen(request, timeout=20) as response:  # bypasses safe_urlopen |
| INT-07 | STILL_OPEN | P2 | `intake/safe_transport.py` | 249,249 | f"GET {path} HTTP/1.1"  # POST unsupported |
| INT-08 | STILL_OPEN | P1 | `intake/closure_review.py` | 21,44 | output = run(str(review_model or DEFAULT_REVIEW_MODEL), prompt)  # no whitelist |
| INT-09 | STILL_OPEN | P2 | `intake/loop.py` | 540,549 | injection detection on up to MAX_LOCAL_READ_BYTES |
| INT-10 | STILL_OPEN | P0 | `intake/loop.py` | 100,105 | if existing is not None and existing.status != "candidate": continue; store.append(record) |
| INT-11 | STILL_OPEN | P1 | `intake/autonomous_sources.py` | 99,114 | latest = records[0] without sort by updated_at |
| INT-12 | STILL_OPEN | P0 | `intake/review.py` | 167,170 | store.append(memory); candidate.status = "promoted"  # non-atomic |
| INT-13 | STILL_OPEN | P1 | `intake/pipeline.py` | 140,142 | candidate+reviewed each limit then [:limit] starves reviewed |
| INT-14 | STILL_OPEN | P2 | `intake/review.py` | 175,189 | mark_candidate_paper_promoted bypasses shared terminal gate |
| INT-15 | STILL_OPEN | P1 | `intake/packs.py` | 60,99 | partial pack failure not retryable |
| INT-16 | STILL_OPEN | P1 | `intake/registry.py` | 155,171 | add_source overwrite clears scan history |
| INT-17 | STILL_OPEN | P1 | `intake/connectors.py` | 396,422 | single category failure drops batch |
| INT-18 | STILL_OPEN | P2 | `intake/source_discovery.py` | 219,221 | undotted classification raises ValueError |
| INT-19 | STILL_OPEN | P1 | `intake/closure_review.py` | 16,39 | review_unavailable collected with no retry path |
| INT-20 | STILL_OPEN | P1 | `intake/loop.py` | 152,175 | per-source mark_source_scanned full registry rewrite |
| INT-21 | STILL_OPEN | P1 | `intake/registry.py` | 173,186 | def list_sources — no mtime cache |
| INT-22 | STILL_OPEN | P1 | `intake/fulltext.py` | 255,275 | _candidate_score re-walks tree |
| INT-23 | STILL_OPEN | P2 | `intake/fulltext.py` | 293,308 | recursive text concat O(size×depth) |
| INT-24 | STILL_OPEN | P2 | `intake/review.py` | 21,41 | multi-status query shares one global limit |
| INT-25 | STILL_OPEN | P2 | `intake/papers/artifacts.py` | 134,134 | text_bytes = path.read_bytes() in loop paths |
| INT-26 | STILL_OPEN | P3 | `intake/` | — | no max_pages hard cap found across intake pagination |
| INT-27 | STILL_OPEN | P3 | `intake/papers/normalize.py` | 106,119 | file-read branch rarely triggered / redundant |

### 3.2 storage

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| STO-01 | STILL_OPEN | P1 | `storage/sqlite_store.py` | 2542,2618 | migration batches still per-row UPDATE in several paths |
| STO-02 | STILL_OPEN | P0 | `storage/sqlite_store.py` | 2579,2600 | SELECT fetchall before BEGIN IMMEDIATE in _apply_source_partition_batch |
| STO-03 | STILL_OPEN | P1 | `storage/runtime_store.py` | 1511,1542 | per-outbox commit/fsync pattern remains |
| STO-04 | STILL_OPEN | P1 | `storage/sqlite_store.py` | 3602,3614 | archival inventory materializes full referenced set |
| STO-05 | STILL_OPEN | P0 | `storage/sqlite_store.py` | 3729,3785 | payload_segments.append before BEGIN; rollback does not delete segment bytes |
| STO-06 | STILL_OPEN | P1 | `storage/sqlite_store.py` | 2440,2460 | processed = apply_batch(...) overwrites rather than accumulates |
| STO-07 | STILL_OPEN | P0 | `storage/runtime_store.py` | 1273,1357 | rebuild_sqlite_from_jsonl os.replace without pre-backup of live DB |
| STO-08 | STILL_OPEN | P2 | `storage/payload_segments.py` | 166,175 | multiple stat/exists per segment file |
| STO-09 | STILL_OPEN | P2 | `storage/sqlite_store.py` | 5954,5954 | re.sub(r"[^\w\u4e00-\u9fff]+"...) — BMP CJK only |
| STO-10 | PARTIAL | P0 | `storage/maintenance.py` | 1253,1298 | offline now required; exclusive SQLite conn still closed before os.replace under file lock |
| STO-11 | STILL_OPEN | P1 | `storage/maintenance.py` | 650,702 | snapshot validation recomputes full SHA-256 |
| STO-12 | STILL_OPEN | P1 | `storage/sqlite_store.py` | 2318,2348 | large single-transaction migration paths remain |
| STO-13 | STILL_OPEN | P2 | `storage/sqlite_store.py` | 3175,3179 | delete migration marker encodes pending archival |
| STO-14 | STILL_OPEN | P2 | `storage/sqlite_store.py` | 4774,4788 | dynamic SQL fragments without centralized allowlist (still literal-sourced) |
| STO-15 | STILL_OPEN | P2 | `storage/jsonl.py` | 96,107 | offset = handle.tell() per line |
| STO-16 | STILL_OPEN | P1 | `storage/payload_segments.py` | 193,229 | tail mismatch triggers full inventory |
| STO-17 | STILL_OPEN | P2 | `storage/atomic_file.py` | 107,128 | mode = existing_stat.st_mode & 0o777 — may inherit wide perms |
| STO-18 | STILL_OPEN | P2 | `storage/sqlite_store.py` | 173,175 | check_same_thread=False without cross-lock assert |
| STO-19 | STILL_OPEN | P2 | `storage/sqlite_store.py` | 5049,5187 | IN clause length unbounded |
| STO-20 | STILL_OPEN | P1 | `storage/jsonl.py` | 858,923 | resegmentation source-length assertion incomplete |
| STO-21 | STILL_OPEN | P2 | `storage/code_evolution_store.py` | 298,323 | nested tx ownership under RLock unreliable |
| STO-22 | STILL_OPEN | P2 | `storage/capability_store.py` | 1043,1053 | at_time correlated subquery index order mismatch |

### 3.3 retrieval

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| RET-01 | STILL_OPEN | P0 | `retrieval/engine.py` | 1189,1386 | create_safety from pre_pool_items[:5000] only |
| RET-02 | STILL_OPEN | P0 | `retrieval/engine.py` | 1938,1945 | and "vector_score" not in hints  # multi-hit penalty |
| RET-03 | STILL_OPEN | P0 | `retrieval/engine.py` | 1733,1740 | if "local_hash_score" not in hint or "dense_vector_score" in hint |
| RET-04 | STILL_OPEN | P1 | `retrieval/fusion.py` | 87,116 | if weight <= 0: continue; weights=only seen_components |
| RET-05 | STILL_OPEN | P1 | `retrieval/incremental_sync.py` | 92,99 | journal fold reachability concerns remain |
| RET-06 | STILL_OPEN | P1 | `retrieval/engine.py` | 915,926 | [\w]+ rule ranking weak on CJK |
| RET-07 | STILL_OPEN | P2 | `retrieval/engine.py` | 1006,1010 | per-item hydration round-trips |
| RET-08 | STILL_OPEN | P1 | `retrieval/postgres_vector.py` | 1356,1358 | request.recall_filter_dict().get("_result_limit") == 1 |
| RET-09 | STILL_OPEN | P2 | `retrieval/engine.py` | 1345,1347 | missing vs zero dense_vector_score indistinguishable via _safe_float |
| RET-10 | STILL_OPEN | P0 | `retrieval/postgres_vector.py` | 1196,1222 | positional %s parallel to condition SQL; DISTINCT ON query builder |
| RET-11 | STILL_OPEN | P1 | `retrieval/sqlite_source.py` | 104,107 | IN ({','.join('?'...)}) unbounded |
| RET-12 | STILL_OPEN | P1 | `retrieval/postgres_vector.py` | 197,211 | embedding response size bound configurable high |
| RET-13 | STILL_OPEN | P2 | `retrieval/relevance.py` | 95,137 | socket timeout not end-to-end budget |
| RET-14 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 1848,1865 | cache key exclusion blacklist style |
| RET-15 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 1019,1026 | missing digest treated as pass (fail-open) |
| RET-16 | STILL_OPEN | P1 | `retrieval/postgres_vector.py` | 908,928 | projection path per-row execute |
| RET-17 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 1212,1220 | rotation merge can overwrite keyword evidence |
| RET-18 | STILL_OPEN | P2 | `retrieval/engine.py` | 1226,1312 | weight parse / record key inside group loop |
| RET-19 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 1103,1165 | new connection + set_config per request |
| RET-20 | STILL_OPEN | P2 | `retrieval/postgres_sync.py` | 318,334 | projection summary computed twice |
| RET-21 | STILL_OPEN | P2 | `retrieval/sqlite_source.py` | 188,188 | loop-invariant work inside loop |
| RET-22 | STILL_OPEN | P3 | `retrieval/postgres_vector.py` | 1571,1571 | health() lockless multi-field snapshot |
| RET-23 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 2372,2405 | local bugs classified as backend faults into circuit |
| RET-24 | STILL_OPEN | P3 | `retrieval/postgres_ddl.py` | 101,129 | HNSW default m/ef_construction |
| RET-25 | STILL_OPEN | P2 | `retrieval/engine.py` | 1441,1461 | bypass_reason compared across request instance state |
| RET-26 | STILL_OPEN | P2 | `retrieval/postgres_vector.py` | 1198,1198 | SELECT DISTINCT ON(p.storage_key) defeats HNSW |
| RET-27 | STILL_OPEN | P2 | `retrieval/engine.py` | 1006,1010 | admission path asdict deep-copies summaries |

### 3.4 recall/scoring/living

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| RSC-01 | STILL_OPEN | P1 | `recall/intent.py` | 96,98 | scores["report"] = max(scores["report"], 0.0) + 0.96 |
| RSC-02 | STILL_OPEN | P1 | `recall/intent.py` | 286,299 | generic+reasons dead/noise branch remains |
| RSC-03 | STILL_OPEN | P2 | `recall/intent.py` | 241,242 | marker in normalized_lower for ... "act" ... — substring |
| RSC-04 | STILL_OPEN | P1 | `recall/intent.py` | 26,31 | operational Chinese regex without total length cap |
| RSC-05 | STILL_OPEN | P2 | `recall/indexing.py` | 26,283 | same-family heuristics diverge across call sites |
| RSC-06 | STILL_OPEN | P2 | `recall/indexing.py` | 97,167 | "news" in source / source_class — hits renews |
| RSC-07 | FIXED | P1 | `recall/lexical.py` | 118,144 | _matching_record_terms linearizes requested set; no cartesian bigram product |
| RSC-08 | STILL_OPEN | P2 | `recall/lexical.py` | 233,239 | phrase_rate can exceed 1 before clamp |
| RSC-09 | STILL_OPEN | P3 | `recall/lexical.py` | 104,104 | re.sub inline in _clean_text not module-level compile |
| RSC-10 | STILL_OPEN | P2 | `recall/indexing.py` | 346,360 | non-overlapping step-2 split vs lexical overlapping compounds |
| RSC-11 | STILL_OPEN | P2 | `recall/loadout.py` | 40,44 | _DROP_TITLE substring discard coupled to exemptions |
| RSC-12 | STILL_OPEN | P1 | `recall/task_queries.py` | 45,51 | sentence regex paths without hard input truncation |
| RSC-13 | STILL_OPEN | P3 | `recall/query_clean.py` | 18,32 | System lines can enter query when tags absent |
| RSC-14 | STILL_OPEN | P1 | `scoring/evaluator.py` | 250,254 | thin_or_noisy truncation not written back |
| RSC-15 | STILL_OPEN | P0 | `scoring/evaluator.py` | 164,169 | legacy_quality.get("confidence") or confidence swallows 0.0 |
| RSC-16 | STILL_OPEN | P1 | `scoring/evaluator.py` | 140,142 | keyword in normalized without word boundary |
| RSC-17 | STILL_OPEN | P1 | `scoring/contract.py` | 38,45 | from_dict float(data.get("value") or 0.0) no type guard |
| RSC-18 | STILL_OPEN | P2 | `scoring/evaluator.py` | 313,357 | empty query can score full |
| RSC-19 | STILL_OPEN | P1 | `scoring/labels.py` | 50,58 | provenance.user_confirmed ordering marks external as confirmed |
| RSC-20 | STILL_OPEN | P2 | `scoring/reports.py` | 20,24 | component_names = tuple(scores[0].components.keys()) |
| RSC-21 | STILL_OPEN | P2 | `scoring/thresholds.py` | 54,58 | unknown profile silent fallback |
| RSC-22 | STILL_OPEN | P0 | `living/schema.py` | 263,287 | let_go early return before repair_needed/trust guards; wait unreachable |
| RSC-23 | STILL_OPEN | P3 | `living/operations.py` | 12,35 | enrich/rewrite without before/after snapshots |

### 3.5 闭环补全

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| EXT-01 | STILL_OPEN | P0 | `api/evolution.py` | 945,1028 | repair_memory_quality → evaluate_memory_quality → store.append (RSC-15 wash) |
| EXT-02 | STILL_OPEN | P0 | `persona/store.py` | 23,41 | state_path.write_text(...); JSON error → default_persona_state() |
| EXT-03 | STILL_OPEN | P1 | `api/memory.py` | 274,278 | capture_decision == "reject": return record  # no append |
| EXT-04 | STILL_OPEN | P1 | `api/memory.py` | 283,293 | _supersede_matching_memories(... limit=20) |
| EXT-05 | STILL_OPEN | P1 | `scheduler/jobs.py` | 33,80 | nightly commits mid-pipeline; later failure leaves partial |
| EXT-06 | STILL_OPEN | P1 | `knowledge/` | — | reconcile/synthesis still O(n²)-ish joins with caps treated ok |
| EXT-07 | STILL_OPEN | P1 | `living/operations.py` | 12,35 | enrich_memory_records single page limit=100, rewrite without CAS |
| EXT-08 | STILL_OPEN | P1 | `identity_ops.py` | 18,40 | repair_hongtu_identity full-table passes without scope |
| EXT-09 | STILL_OPEN | P1 | `scheduler/jobs.py` | 7,41 | import tracemalloc; forced tracing in nightly |
| EXT-10 | STILL_OPEN | P1 | `embeddings/local.py` | 10,33 | VECTOR_SIZE=128; hashlib.sha1 trigram embedding |
| EXT-11 | STILL_OPEN | P1 | `knowledge/projectors.py` | 19,84 | refresh/projector materializes unbounded working sets |
| EXT-12 | STILL_OPEN | P1 | `experience/` | — | outcome fallback full table scan paths remain |
| EXT-13 | STILL_OPEN | P1 | `raw/retrieval.py` | 446,446 | urllib.request.urlopen for rerank; multi-path fallback |
| EXT-14 | STILL_OPEN | P2 | `api/evolution.py` | 864,910 | memory_quality_report under-reads business_metadata |
| EXT-15 | STILL_OPEN | P2 | `persona/store.py` | 37,41 | persona_snapshots write with no retention cap |
| EXT-16 | STILL_OPEN | P2 | `living/schema.py` | 264,264 | RSC-22 source still present (cross-ref) |
| EXT-17 | STILL_OPEN | P2 | `recall/intent.py` | 97,97 | RSC-01 source still present (cross-ref) |
| EXT-18 | STILL_OPEN | P2 | `scoring/labels.py` | 51,51 | RSC-19 source still present (cross-ref) |
| EXT-19 | STILL_OPEN | P2 | `scoring/contract.py` | 42,42 | RSC-17 source still present (cross-ref) |
| EXT-20 | STILL_OPEN | P2 | `raw/store.py` | 45,90 | raw_chunk scan paths with 5000-class caps |
| EXT-21 | STILL_OPEN | P2 | `api/memory.py` | 181,278 | ingest may write before identity closure when id absent |
| EXT-22 | STILL_OPEN | P3 | `living/operations.py` | 80,120 | summarize/posture helpers do not persist |
| EXT-23 | STILL_OPEN | P3 | `embeddings/local.py` | 10,10 | local 128-dim vs PG 1536 incomparable |

### 3.6 治理/适配

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| GOV-01 | STILL_OPEN | P1 | `governance/safety/kill_switch.py` | 46,57 | subprocess.run(["pkill", "-9", "-f", "eimemory"]... |
| GOV-02 | STILL_OPEN | P1 | `governance/deployment_receipt.py` | 737,737 | with urlopen(url, timeout=5) as response:  # no SSRF pin |
| GOV-03 | STILL_OPEN | P1 | `governance/promotion_manager.py` | 1010,1023 | store_rule(..., status="active") for memory_rule |
| GOV-04 | STILL_OPEN | P1 | `governance/promotion_manager.py` | 956,956 | min(0.95, max(0.75, ... confidence ...)) |
| GOV-05 | STILL_OPEN | P2 | `governance/safety/kill_switch.py` | 57,80 | kill before audit write; self-kill loses audit |
| GOV-06 | STILL_OPEN | P2 | `governance/l5_loop.py` | 55,76 | build_world_model(..., limit: int = 500) treated as authoritative |
| GOV-07 | STILL_OPEN | P2 | `governance/autonomous_learning.py` | 227,236 | build_self_model(..., persist=True) before later gates |
| GOV-08 | STILL_OPEN | P3 | `storage/migrations/backfill_capability_v3.py` | 12,14 | retry may repeat committed work; relies on request-identity idempotency |

### 3.6b 适配器

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| ADP-01 | STILL_OPEN | P1 | `adapters/runtime/http_client.py` | 63,69 | Bearer + urllib.request.urlopen without safe_transport |
| ADP-02 | STILL_OPEN | P1 | `adapters/openclaw/hooks.py` | 726,735 | except Exception: return self._empty_bundle(...) |

### 3.7 召回专项 RC

| 编号 | 状态 | 严重度 | 路径 | 行号 | 证据摘要 |
| --- | --- | --- | --- | --- | --- |
| RC-01 | STILL_OPEN | P1 | `recall/intent.py` | 96,98 | alias RSC-01 report += 0.96；↔ RSC-01 |
| RC-02 | STILL_OPEN | P2 | `recall/intent.py` | 241,242 | alias RSC-03 act substring；↔ RSC-03 |
| RC-03 | STILL_OPEN | P2 | `recall/intent.py` | 137,279 | news/paper cues without token boundaries |
| RC-04 | STILL_OPEN | P2 | `recall/intent.py` | 64,70 | task_mode confidence 0.96 early return suppresses kinds |
| RC-05 | STILL_OPEN | P2 | `recall/intent.py` | 295,298 | generic+reasons dead branch |
| RC-06 | STILL_OPEN | P1 | `recall/intent.py` | 26,31 | alias RSC-04；↔ RSC-04 |
| RC-07 | STILL_OPEN | P2 | `recall/lexical.py` | 233,239 | alias RSC-08；↔ RSC-08 |
| RC-08 | STILL_OPEN | P3 | `recall/lexical.py` | 104,104 | alias RSC-09；↔ RSC-09 |
| RC-09 | STILL_OPEN | P3 | `recall/lexical.py` | 160,160 | Chinese substring 中国 hits 中国人 |
| RC-10 | STILL_OPEN | P1 | `recall/indexing.py` | 97,167 | alias RSC-06；↔ RSC-06 |
| RC-11 | STILL_OPEN | P2 | `recall/indexing.py` | 161,161 | title contains health → diagnostic |
| RC-12 | STILL_OPEN | P2 | `recall/indexing.py` | 346,360 | alias RSC-10 tokenizer divergence；↔ RSC-10 / RET-06 |
| RC-13 | STILL_OPEN | P2 | `recall/indexing.py` | 270,283 | outcome/episode family heuristics split |
| RC-14 | STILL_OPEN | P1 | `recall/loadout.py` | 40,44 | arxiv/locomo substring drops memories；↔ RSC-11 |
| RC-15 | STILL_OPEN | P2 | `recall/loadout.py` | 50,51 | persona duplicates items |
| RC-16 | STILL_OPEN | P2 | `recall/loadout.py` | 80,80 | endswith(summary) false positive |
| RC-17 | STILL_OPEN | P1 | `recall/task_queries.py` | 45,51 | alias RSC-12；↔ RSC-12 |
| RC-18 | STILL_OPEN | P2 | `recall/task_queries.py` | 73,90 | ambiguous treated as empty store |
| RC-19 | STILL_OPEN | P2 | `recall/query_clean.py` | 18,32 | System lines enter query；↔ RSC-13 |
| RC-20 | STILL_OPEN | P0 | `retrieval/engine.py` | 1938,1945 | alias RET-02 keyword eligibility；↔ RET-02 |
| RC-21 | STILL_OPEN | P0 | `retrieval/engine.py` | 1733,1740 | alias RET-03 standalone hash grounding；↔ RET-03 |
| RC-22 | STILL_OPEN | P0 | `retrieval/engine.py` | 1189,1386 | alias RET-01 create_safety pool；↔ RET-01 |
| RC-23 | STILL_OPEN | P1 | `retrieval/postgres_vector.py` | 1356,1358 | alias RET-08；↔ RET-08 |
| RC-24 | STILL_OPEN | P2 | `retrieval/fusion.py` | 87,116 | alias RET-04 zero-weight still in audit map asymmetry；↔ RET-04 |
| RC-25 | FIXED | P1 | `retrieval/engine.py` | 403,415 | if limit <= 0: return empty bundle with invalid_request=non_positive_limit |
| RC-26 | STILL_OPEN | P1 | `adapters/openclaw/hooks.py` | 726,735 | alias ADP-02；↔ ADP-02 |
| RC-27 | STILL_OPEN | P1 | `adapters/openclaw/hooks.py` | 1460,1469 | salience = _float_or_zero(salience_score or importance) |
| RC-28 | STILL_OPEN | P1 | `retrieval/proactive.py` | 614,641 | decision persist fail → mandatory_fallback fail-open |

---

## 四、新发现 NEW-01…

### NEW-01 ｜ 提示安全远程调用携带 Bearer 走原生 urlopen

| 字段 | 内容 |
| --- | --- |
| 严重度 | P1 |
| 位置 | `governance/prompt_safety_remote.py` L401,407 |

**机制**：EIMEMORY_PROMPT_SAFETY_API_KEY 以 Authorization: Bearer 发往 base_url/chat/completions，使用 urllib.request.urlopen，无 DNS pinning / peer 复核 / 逐跳重定向校验。与 INT-06、ADP-01、EXT-13、GOV-02 同族旁路。

**修复**：改走 intake.safe_transport（需先补 POST，见 INT-07）或共享安全 HTTP 出口；限制 base_url 为显式 allowlist。

### NEW-02 ｜ 桥接监视器默认私网地址 + 无 SSRF 钉扎的 urlopen

| 字段 | 内容 |
| --- | --- |
| 严重度 | P1 |
| 位置 | `ei_bridge/eibrain_monitor.py` L10,41 |

**机制**：DEFAULT_MONITOR_URL = http://100.81.78.119:18080/status.json；_fetch_status 直接 request.urlopen(self.monitor_url)。环境变量可改写为任意地址，无地址族约束。

**修复**：默认改为 loopback 或强制配置；所有出口经 safe_urlopen；拒绝默认硬编码可达私网。

### NEW-03 ｜ 飞书 webhook 原生 urlopen（ops 双处）

| 字段 | 内容 |
| --- | --- |
| 严重度 | P2 |
| 位置 | `ops/timer_monitor.py` L269,273 |

**机制**：ops/timer_monitor.py 与 ops/openclaw_loop.py 的 _post_feishu_webhook 对运营配置 URL 直接 urlopen，跟随重定向，无 SSRF 钉扎；异常吞掉返回 False。

**修复**：webhook URL 校验方案+host allowlist，或走 safe_urlopen；失败应可观测。

### NEW-04 ｜ 部署脚本下载路径原生 urlopen

| 字段 | 内容 |
| --- | --- |
| 严重度 | P2 |
| 位置 | `deploy/provision_reranker.py` L28,28 |

**机制**：deploy/provision_reranker.py、prepare_qwen_reranker.py、activate_reranker_artifact.py 使用 urllib.request.urlopen 拉取外部工件，无与 safe_transport 对等的钉扎。部署上下文风险低于运行时，但同库原语未复用。

**修复**：部署下载统一 pinning 或校验 checksum+HTTPS pin；与运行时出口策略对齐。

### NEW-05 ｜ Hermes 投递路径宽泛 except 后静默返回

| 字段 | 内容 |
| --- | --- |
| 严重度 | P2 |
| 位置 | `adapters/hermes/channel_delivery.py` L130,135 |

**机制**：channel_delivery 多处 except Exception: 后 return/None，宿主侧可把投递失败读成「无需投递」。provider_core 对部分错误 similarly 吞掉以保 worker 存活，需与 ADP-02 同类显式 unavailable 语义对齐。

**修复**：区分 transport_error vs empty；向上返回 status=unavailable 而非空成功。

---

## 五、RC ↔ 登记册交叉引用

RC 与 RET/RSC/ADP 描述同一机制时，**头数只计登记册一侧**；两端 ID 均保留在矩阵中。

| RC | 登记册 | 主题 |
| --- | --- | --- |
| RC-01 | RSC-01 | report 累加 |
| RC-02 | RSC-03 | act 子串 |
| RC-06 | RSC-04 | 运营正则长度 |
| RC-07 | RSC-08 | phrase_rate |
| RC-08 | RSC-09 | clean_text compile |
| RC-10 | RSC-06 | news in source |
| RC-12 | RSC-10 / RET-06 | 中文切分分裂 |
| RC-14 | RSC-11 | loadout 丢弃 |
| RC-17 | RSC-12 | 句子正则 |
| RC-19 | RSC-13 | query_clean System |
| RC-20 | RET-02 | keyword 资格 |
| RC-21 | RET-03 | 哈希 grounding |
| RC-22 | RET-01 | create_safety |
| RC-23 | RET-08 | limit==1 |
| RC-24 | RET-04 | 零权审计 |
| RC-26 | ADP-02 | 召回异常空包 |

其余 RC-03..05, RC-09, RC-11, RC-13, RC-15..16, RC-18, RC-27, RC-28 为召回专项独有编号，计入 RC 矩阵但不并入 INT/STO/… 头数。

---

## 六、更新修复列车（仅 STILL_OPEN / PARTIAL）

| 车次 | 编号 | 动作 |
| --- | --- | --- |
| A0 阻断 | INT-03 RET-10 RSC-17 EXT-02 ADP-01 EXT-13 GOV-02 **NEW-01 NEW-02** | 根白名单、命名参数、类型防御、原子人格写、HTTP 出口统一 safe_transport（含 POST） |
| A1 正确性 | RSC-15 EXT-01 RET-02 RET-03 RET-01 RSC-22 INT-10 INT-12 | 零值 is None、显式臂标记、权威存在性、状态机守卫、幂等晋升 |
| A2 耐久 | STO-07 STO-05 STO-02 STO-10(PARTIAL) | 重建备份、回收 segment、事务内读或 CAS、VACUUM 替换期保持独占 |
| B0 吞吐 | STO-03 STO-01 RET-16 EXT-09 | 批量 commit、executemany、去掉 nightly tracemalloc |
| B1 调度闭环 | EXT-05 EXT-03 EXT-04 GOV-03 GOV-04 ADP-02 RC-26..28 | 分步报告、拒绝落盘、扫尽 semantic_key、规则先 shadow、召回失败显式化 |
| B2 安全加固 | INT-04 INT-06 INT-07 INT-02 INT-08 GOV-01 NEW-03 NEW-04 | 密钥正则、POST 安全传输、XML 上限、kill 改 PID/cgroup、webhook/部署下载钉扎 |
| C 规模 | EXT-10 EXT-11 EXT-12 RET-19 RET-26 STO-04 STO-16 NEW-05 | 嵌入限流、流式刷新、连接池、HNSW 近邻、归档分批、Hermes 失败语义 |

> RSC-07、RC-25 已 FIXED，移出列车。

---

## 七、应保留的正面发现

1. intake/safe_transport.py：DNS pinning、peer IP、逐跳重定向、地址归一化（仍为标杆，但未被全库复用）。
2. intake/papers/artifacts.py：三层摘要、relative_to(root)、拒 symlink。
3. storage/payload_segments.py + jsonl manifest + vacuum journal + capability SAVEPOINT+CAS。
4. storage 动态 SQL 片段可追溯到硬编码字面量；未见可利用注入。
5. retrieval 凭据 repr=False、dsn 只存 sha256、输出白名单。
6. api/memory.record_memory_usage：事务内按 idempotency_key 再查再 upsert。
7. evaluation.real_query_gate：无基线/不合格 → not_run，fail-closed（应保持）。
8. recall/lexical._matching_record_terms：已消除二字笛卡尔积（RSC-07 FIXED，勿回退）。
8b. retrieval/engine：`limit <= 0` 早退空包并标 `non_positive_limit`（RC-25 FIXED）。
9. adapters/hermes/code_implementation.py 与 native_memory：路径归一化、拒 ..、拒 symlink、原子临时文件写入——正面范式。
10. STO-10：vacuum_into_atomic 现强制 offline=True（相对前版加强；替换阶段锁仍有缺口 → PARTIAL）。

**弱化说明**：`safe_transport` 标杆仍在，但本轮新确认 `prompt_safety_remote`、`eibrain_monitor`、ops webhook、deploy 下载继续旁路——「原语未成为唯一出口」进一步恶化，而非削弱原语本身质量。

---

## 八、剩余盲区

- `governance/code_evolution_effects.py`、`release_lineage.py`、`dynamic_capability_evolution.py`、`l5_readiness.py` 后半：模式扫描为主，未逐函数证明。
- `cli/main.py`（约 3.4k 行）命令分发：确认大量 `write_text`/`open` 由操作员路径驱动，未逐子命令做授权模型证明。
- `evaluation/` 除 real_query_gate 外基准集未逐行。
- `deploy/systemd` 单元与凭据旋转脚本未做运行时权限演练。
- 未做动态 PoC / 性能基准；行号以 2026-09-16 box 拷贝为准。

---

*REAUDIT 2026-09-16 · eimemory 1.13.11 · 登记册全量复核 · 新发现 5 · 前册正式 P0 无 FIXED*