# Lightweight evidence recall

This optional policy uses the existing embedding endpoint and PostgreSQL. It
does not start a reranker, LLM or graph service, and does not mutate original
memories. SQLite remains authoritative. Fragment offsets address the exact
parent keyword projection, bound to scope, source, parent digest and watermark.
The parent row's update/delete and generation replacement cascade to fragments.

Enable only on an isolated candidate index first:

- `EIMEMORY_POSTGRES_EVIDENCE_FRAGMENTS=1`
- `EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED=1`
- `EIMEMORY_RERANKER_ENABLED=0`

Rebuild the derived index after changing projection policy. Old embeddings are
not silently reused across projection fingerprints. Sentence-based extractive
spans preserve original text; long sentences use overlapping windows instead of
head/tail splicing. Dense and full-text search reserve candidate slots before
local admission. A shared versioned Unicode/CJK-bigram tokenizer handles both
query and PostgreSQL document text without a resident model or dictionary.

Admission uses raw cosine and lexical coverage, not RRF as a confidence score.
Its configuration is unvalidated by default. Calibrate only on development
cases, pass known regressions, then run the untouched holdout. Existing quality,
negative, forbidden-reference and latency gates are unchanged. Natural Codex
evidence and the production/strict reports are separate release requirements.

`tests/test_lightweight_postgres_integration.py` exercises real PostgreSQL
bootstrap, scope isolation, changed spans, delete cascade and stale CAS with a
synthetic three-dimensional provider. It requires `EIMEMORY_TEST_POSTGRES_DSN`
and creates/removes only its own UUID-named test tables; it is not quality proof.

The following source reviews informed this independently implemented Python
design; no upstream source file or framework was copied:

- TencentDB-Agent-Memory (`220af62226221d7ea07f95aa61eb7c0486cb407a`): consistent
  Chinese document/query processing, hybrid recall and source-bound memories.
- Hindsight (`9e6d9e76cc52e918b24440355759991722a758a2`): per-arm candidate retention
  and preservation of original retrieval signals.
- Graphiti (`b943c9e8486cdc7fe6cb2f4cfe151ae53f0a884d`): separate optional ranking
  strategies. This implementation uses conservative lexical duplicate
  suppression, not Graphiti's graph traversal or cross-encoder.
- Mem0 (`dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3`): write-side duplicate control.
  No new generative extraction is introduced here.
