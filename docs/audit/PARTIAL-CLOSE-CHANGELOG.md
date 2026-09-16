# PARTIAL Close Changelog — eimemory 1.13.13 → 1.13.14

Date: 2026-09-16 (Asia/Shanghai / CST+8)  
Tree: `/workspace/eimemory-review/src/`  
Requirement: 核对两份原始审计报告后「没有要修复到完成」— annotations-only is NOT enough.

## Summary

| Metric | Before | After |
| --- | ---: | ---: |
| FIXED | 115 | **160** |
| PARTIAL | 45 | **0** |
| STILL_OPEN | 0 | **0** |
| Version | 1.13.13 | **1.13.14** |

## Closed IDs (code behavior)

### Intake (5)
INT-20, INT-23, INT-25, INT-26, INT-27

### Storage (12)
STO-04, STO-08, STO-11, STO-12, STO-13, STO-14, STO-16, STO-18, STO-20, STO-21, STO-22

### Retrieval (18)
RET-05, RET-07, RET-09, RET-12, RET-13, RET-14, RET-17, RET-18, RET-19, RET-20, RET-21, RET-22, RET-23, RET-24, RET-25, RET-26, RET-27

### Recall / living (2)
RSC-05, RSC-23

### EXT (10)
EXT-06, EXT-07, EXT-08, EXT-11, EXT-12, EXT-14, EXT-20, EXT-21, EXT-22, EXT-23

## Key behavioral changes (not comments)

- Batch source scan markers; bounded HTML text depth; streaming artifact compare; HARD_MAX_PAGES + inventory walk page ceilings; gated PDF hashing
- Paged archival inventory; cached/single stat; mtime short-circuit hashing; offline migration default; pending_archival flag; SQL allowlist; lock assert wired into upsert/get_by_id; source-length resegment assert; nested-tx depth; index-aligned at_time ORDER BY
- Journal unreachable fold; batch hydrate; key-in dense scores; 2MiB/8MiB embed caps; deadline budget; allowlist cache keys; keyword evidence preserve; hoisted loops; locked health; local-bug circuit exclusion; HNSW params; request-local bypass; ANN-then-filter; no asdict admission; idle PG pool
- Shared same_family_record; enrich digests; keyed reconcile+incomplete; multi-page CAS enrich; scoped identity repair; streamed projectors; scoped outcome fallback; business_metadata quality; raw scan caps; identity-before-write ingest; optional summarize persist; local/PG dim incompatibility flag

## Tests

```bash
cd /workspace/eimemory-review/src
../venv/bin/python -m pytest tests/test_a0_a2_remediation.py tests/test_remaining_remediation.py tests/test_version.py tests/test_partial_close.py -q --tb=short
```

Result (2026-09-16 CST+8): **47 passed**

## Paths

- Code: `/workspace/eimemory-review/src/eimemory/`
- VERIFY: `/workspace/eimemory-review/VERIFY-vs-original-audits.md`
- CHANGELOG: `/workspace/eimemory-review/src/CHANGELOG.md` (`## [1.13.14]`)
- This file: `/workspace/eimemory-review/PARTIAL-CLOSE-CHANGELOG.md`
