# EIMemory LongMemEval Raw Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build eimemory `0.3.0` around a raw evidence layer, a reproducible LongMemEval adapter, and two-stage recall so long-term memory keeps original evidence before deriving structured memories.

**Architecture:** Add a first-class `raw_chunk` evidence record type that stores verbatim conversation/document/event text with context-window links. Extend existing SQLite-backed search and evaluation modules instead of introducing ChromaDB in the first release. LongMemEval runs should measure raw retrieval first, then hybrid raw+structured recall, with clear separation between retrieval R@k and end-to-end QA-style answer checks.

**Tech Stack:** Python 3.13-compatible codebase, existing `Runtime`, `RuntimeStore`, `MemoryAPI`, `RecordEnvelope`, SQLite vector-ish hybrid scoring, pytest, existing CLI parser in `eimemory/cli/main.py`, JSON/JSONL datasets.

---

## Version Decision

Bump eimemory from `0.2.6` to `0.3.0`.

Reason: this adds a new durable record kind, new ingestion path, public CLI behavior, new report schema, and a changed recall architecture. In a pre-1.0 package, this is a minor release, not a patch.

Files:
- `pyproject.toml`: `version = "0.3.0"`
- `eimemory/version.py`: `__version__ = "0.3.0"`
- `tests/test_version.py`: assert `"0.3.0"`

## External References

Use MemPalace as evidence for the raw-first direction, not as an architecture to clone.

- MemPalace README states the core design: store conversation history as verbatim text, retrieve with semantic search, and avoid summarization/extraction as the primary memory path.
- MemPalace benchmark docs explicitly distinguish retrieval recall from end-to-end QA accuracy. eimemory reports must keep that distinction.
- MemPalace LongMemEval runner shows the useful baseline shape: build a corpus from `haystack_sessions`, retrieve against `question`, score against gold session ids, and emit R@k/NDCG breakdowns.

References:
- `https://github.com/MemPalace/mempalace`
- `https://raw.githubusercontent.com/MemPalace/mempalace/develop/benchmarks/BENCHMARKS.md`
- `https://raw.githubusercontent.com/MemPalace/mempalace/develop/benchmarks/longmemeval_bench.py`

## Non-Goals

- Do not add ChromaDB as a hard dependency in `0.3.0`.
- Do not headline R@5 as QA accuracy.
- Do not add benchmark-specific hacks for individual LongMemEval misses.
- Do not replace existing `memory`, `claim_card`, or `knowledge_page` records.
- Do not build Wing/Room/Closet/Drawer UI structures in P0. Palace-like grouping can be a later soft-scope layer.
- Do not require an LLM or API key for the raw LongMemEval path.

## Target Outcome

P0 acceptance:
- Every conversation/document/event ingestion path can persist verbatim chunks.
- Each structured memory can link back to raw evidence.
- A raw chunk can return a context window by `record_id`.
- `eimemory eval longmem DATASET` can run a small LongMemEval-compatible dataset.
- The report includes `retrieval_recall_at_1`, `retrieval_recall_at_5`, `retrieval_recall_at_10`, `ndcg_at_5`, `mrr`, latency, and per-question-type breakdown.
- Two-stage recall can return a raw evidence bundle and linked structured records.

P1 acceptance:
- Reranking includes keyword overlap, quoted phrase boost, proper noun boost, temporal proximity, speaker/role signal, and preference pattern synthetic evidence.
- Conflict handling supports old/current fact ranking with validity windows.

## File Structure

Create:
- `eimemory/raw/__init__.py`
  - Export `RawEvidenceAPI`, chunk helpers, and context-window helpers.
- `eimemory/raw/chunks.py`
  - Deterministic chunking, text hashing, role/session metadata normalization.
- `eimemory/raw/store.py`
  - `RawEvidenceAPI` for ingesting raw text/conversation turns and reading context windows.
- `eimemory/raw/retrieval.py`
  - Raw chunk search and deterministic rerank helpers.
- `eimemory/evaluation/longmemeval.py`
  - LongMemEval dataset adapter, raw ingestion, retrieval scoring, report builder.
- `examples/evaluation/longmemeval_smoke.json`
  - Tiny two-question smoke dataset that mirrors the LongMemEval shape.
- `tests/test_raw_evidence_store.py`
  - Raw chunk record, context window, and provenance tests.
- `tests/test_longmemeval_adapter.py`
  - Dataset normalization, raw retrieval metrics, and CLI-style report tests.
- `tests/test_two_stage_recall.py`
  - Raw-first recall and evidence bundle tests.

Modify:
- `eimemory/models/records.py`
  - Add `raw_chunk` to `VALID_KINDS`.
- `eimemory/api/runtime.py`
  - Add `self.raw = RawEvidenceAPI(store)`.
  - Add `run_longmemeval(...)`.
- `eimemory/api/memory.py`
  - Add opt-in two-stage raw recall mode.
- `eimemory/storage/sqlite_store.py`
  - Include `record_id` and raw text fields in `content_text` indexing so direct chunk/session ids are searchable.
- `eimemory/evaluation/metrics.py`
  - Add `recall_any_at_k`, `recall_all_at_k`, and ranking helpers that work on session ids and chunk ids.
- `eimemory/evaluation/__init__.py`
  - Export LongMemEval runner.
- `eimemory/cli/main.py`
  - Add `eimemory eval longmem DATASET --mode raw|hybrid --granularity session|turn|chunk --limit N --output PATH`.
- `eimemory/governance/snapshot.py`
  - Surface latest LongMemEval report.
- `docs/evaluation.md`
  - Document raw evidence and LongMemEval usage.

## Data Contracts

### Raw Chunk Record

`raw_chunk` records are ordinary `RecordEnvelope` records.

```json
{
  "kind": "raw_chunk",
  "title": "Raw chunk sess_001#0003",
  "summary": "verbatim first 240 chars",
  "detail": "full verbatim chunk text",
  "content": {
    "text": "full verbatim chunk text",
    "source_event_id": "lme_case_001",
    "source_type": "conversation",
    "session_id": "sess_001",
    "turn_id": "sess_001_turn_03",
    "role": "assistant",
    "speaker": "assistant",
    "chunk_index": 3,
    "prev_chunk_id": "raw_...",
    "next_chunk_id": "raw_...",
    "raw_text_hash": "sha256...",
    "occurred_at": "2025-01-03T10:00:00Z"
  },
  "tags": ["raw-evidence", "conversation"],
  "source": "eimemory.raw.ingest",
  "meta": {
    "evidence_layer": "raw",
    "granularity": "turn",
    "token_estimate": 80
  }
}
```

### LongMemEval Report

Persist LongMemEval reports as `kind="reflection"` with `source="eimemory.longmemeval"` and `meta.report_type="longmemeval_eval"`.

```json
{
  "ok": true,
  "schema_version": 1,
  "report_type": "longmemeval_eval",
  "name": "longmemeval-smoke",
  "mode": "raw",
  "granularity": "session",
  "sample_count": 2,
  "retrieval": {
    "recall_any_at_1": 0.5,
    "recall_any_at_5": 1.0,
    "recall_all_at_5": 1.0,
    "ndcg_at_5": 0.815,
    "mrr": 0.75
  },
  "efficiency": {
    "latency_ms_avg": 0.0,
    "latency_ms_p95": 0.0,
    "ingested_raw_chunk_count": 0
  },
  "by_question_type": {
    "temporal_reasoning": {"sample_count": 1, "recall_any_at_5": 1.0}
  },
  "samples": []
}
```

## Task 1: Add `raw_chunk` Record Kind

**Files:**
- Modify: `eimemory/models/records.py`
- Test: `tests/test_raw_evidence_store.py`

- [ ] **Step 1: Write the failing record-kind test**

Add:

```python
from eimemory.models.records import RecordEnvelope, ScopeRef, VALID_KINDS


def test_raw_chunk_is_a_valid_record_kind() -> None:
    assert "raw_chunk" in VALID_KINDS
    record = RecordEnvelope.create(
        kind="raw_chunk",
        title="Raw chunk sess-1#0",
        summary="User said they prefer PostgreSQL.",
        detail="User said they prefer PostgreSQL because backups are easier.",
        content={
            "text": "User said they prefer PostgreSQL because backups are easier.",
            "session_id": "sess-1",
            "chunk_index": 0,
            "raw_text_hash": "hash",
        },
        scope=ScopeRef(agent_id="hongtu", workspace_id="embodied"),
        source="eimemory.raw.ingest",
    )
    assert record.kind == "raw_chunk"
    assert record.content["session_id"] == "sess-1"
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_raw_chunk_is_a_valid_record_kind -q
```

Expected: fails with `ValueError: invalid record kind: raw_chunk`.

- [ ] **Step 3: Add the kind**

In `eimemory/models/records.py`, add `"raw_chunk"` to `VALID_KINDS`.

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_raw_chunk_is_a_valid_record_kind -q
```

Expected: `1 passed`.

## Task 2: Build Deterministic Raw Chunking

**Files:**
- Create: `eimemory/raw/__init__.py`
- Create: `eimemory/raw/chunks.py`
- Test: `tests/test_raw_evidence_store.py`

- [ ] **Step 1: Write failing chunking tests**

Append:

```python
from eimemory.raw.chunks import chunk_text, raw_text_hash


def test_chunk_text_is_deterministic_and_overlapping() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    chunks = chunk_text(
        text,
        session_id="sess-1",
        source_event_id="event-1",
        role="user",
        speaker="alice",
        max_chars=24,
        overlap_chars=6,
    )

    assert [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0]["session_id"] == "sess-1"
    assert chunks[0]["source_event_id"] == "event-1"
    assert chunks[0]["role"] == "user"
    assert chunks[0]["speaker"] == "alice"
    assert chunks[0]["text"]
    assert chunks[1]["text"].startswith(chunks[0]["text"][-6:].strip()[:1])
    assert chunk_text(text, session_id="sess-1", source_event_id="event-1") == chunk_text(
        text,
        session_id="sess-1",
        source_event_id="event-1",
    )


def test_raw_text_hash_is_stable() -> None:
    assert raw_text_hash("  hello\nworld  ") == raw_text_hash("hello world")
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_chunk_text_is_deterministic_and_overlapping tests/test_raw_evidence_store.py::test_raw_text_hash_is_stable -q
```

Expected: import error for `eimemory.raw`.

- [ ] **Step 3: Implement `chunks.py`**

Create `eimemory/raw/chunks.py`:

```python
from __future__ import annotations

import hashlib
from typing import Any


def normalize_raw_text(text: str) -> str:
    return " ".join(str(text or "").split())


def raw_text_hash(text: str) -> str:
    return hashlib.sha256(normalize_raw_text(text).encode("utf-8")).hexdigest()


def chunk_text(
    text: str,
    *,
    session_id: str,
    source_event_id: str,
    role: str = "",
    speaker: str = "",
    occurred_at: str = "",
    max_chars: int = 1200,
    overlap_chars: int = 160,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_raw_text(text)
    if not normalized:
        return []
    max_chars = max(1, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars), max_chars - 1))
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(
                {
                    "text": chunk,
                    "session_id": str(session_id or ""),
                    "source_event_id": str(source_event_id or ""),
                    "role": str(role or ""),
                    "speaker": str(speaker or ""),
                    "occurred_at": str(occurred_at or ""),
                    "chunk_index": len(chunks),
                    "raw_text_hash": raw_text_hash(chunk),
                    **dict(extra or {}),
                }
            )
        if end >= len(normalized):
            break
        start = max(0, end - overlap_chars)
    return chunks
```

Create `eimemory/raw/__init__.py`:

```python
from eimemory.raw.chunks import chunk_text, normalize_raw_text, raw_text_hash

__all__ = ["chunk_text", "normalize_raw_text", "raw_text_hash"]
```

- [ ] **Step 4: Verify green**

Run the two tests again. Expected: both pass.

## Task 3: Add RawEvidenceAPI Ingestion And Context Windows

**Files:**
- Create: `eimemory/raw/store.py`
- Modify: `eimemory/raw/__init__.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/test_raw_evidence_store.py`

- [ ] **Step 1: Write failing API tests**

Append:

```python
from eimemory.api.runtime import Runtime


def test_raw_evidence_api_persists_chunks_and_context_window(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}

    records = runtime.raw.ingest_text(
        text="first message. second message. third message.",
        scope=scope,
        source_event_id="event-1",
        session_id="sess-1",
        role="user",
        speaker="alice",
        max_chars=18,
        overlap_chars=4,
    )

    assert len(records) >= 2
    assert all(record.kind == "raw_chunk" for record in records)
    assert records[0].content["next_chunk_id"] == records[1].record_id
    assert records[1].content["prev_chunk_id"] == records[0].record_id

    window = runtime.raw.context_window(records[1].record_id, scope=scope, radius=1)
    assert [item.record_id for item in window] == [records[0].record_id, records[1].record_id]
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_raw_evidence_api_persists_chunks_and_context_window -q
```

Expected: `AttributeError: 'Runtime' object has no attribute 'raw'`.

- [ ] **Step 3: Implement `RawEvidenceAPI`**

Create `eimemory/raw/store.py` with:

```python
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.chunks import chunk_text
from eimemory.storage.runtime_store import RuntimeStore


class RawEvidenceAPI:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def ingest_text(
        self,
        *,
        text: str,
        scope: dict,
        source_event_id: str,
        session_id: str,
        source_type: str = "conversation",
        role: str = "",
        speaker: str = "",
        occurred_at: str = "",
        max_chars: int = 1200,
        overlap_chars: int = 160,
        meta: dict[str, Any] | None = None,
    ) -> list[RecordEnvelope]:
        scope_ref = ScopeRef.from_dict(scope)
        chunk_payloads = chunk_text(
            text,
            session_id=session_id,
            source_event_id=source_event_id,
            role=role,
            speaker=speaker,
            occurred_at=occurred_at,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            extra={"source_type": source_type},
        )
        records = [
            RecordEnvelope.create(
                kind="raw_chunk",
                title=f"Raw chunk {session_id}#{payload['chunk_index']}",
                summary=str(payload["text"])[:240],
                detail=str(payload["text"]),
                content=payload,
                tags=["raw-evidence", source_type],
                source="eimemory.raw.ingest",
                scope=scope_ref,
                meta={
                    "evidence_layer": "raw",
                    "granularity": "chunk",
                    "token_estimate": max(1, len(str(payload["text"]).split())),
                    **dict(meta or {}),
                },
            )
            for payload in chunk_payloads
        ]
        for index, record in enumerate(records):
            if index > 0:
                record.content["prev_chunk_id"] = records[index - 1].record_id
            if index + 1 < len(records):
                record.content["next_chunk_id"] = records[index + 1].record_id
        return [self.store.append(record) for record in records]

    def context_window(self, record_id: str, *, scope: dict, radius: int = 1) -> list[RecordEnvelope]:
        center = self.store.get_by_id(record_id, scope=scope)
        if center is None or center.kind != "raw_chunk":
            return []
        session_id = str(center.content.get("session_id") or "")
        center_index = int(center.content.get("chunk_index") or 0)
        lower = center_index - max(0, int(radius))
        upper = center_index + max(0, int(radius))
        records = [
            record
            for record in self.store.list_records(kinds=["raw_chunk"], scope=scope, limit=1000)
            if str(record.content.get("session_id") or "") == session_id
            and lower <= int(record.content.get("chunk_index") or 0) <= upper
        ]
        return sorted(records, key=lambda item: int(item.content.get("chunk_index") or 0))
```

Update `eimemory/raw/__init__.py`:

```python
from eimemory.raw.chunks import chunk_text, normalize_raw_text, raw_text_hash
from eimemory.raw.store import RawEvidenceAPI

__all__ = ["RawEvidenceAPI", "chunk_text", "normalize_raw_text", "raw_text_hash"]
```

Update `Runtime.__init__`:

```python
from eimemory.raw.store import RawEvidenceAPI

self.raw = RawEvidenceAPI(store)
```

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py -q
```

Expected: raw evidence tests pass.

## Task 4: Index Raw IDs And Raw Text For Search

**Files:**
- Modify: `eimemory/storage/sqlite_store.py`
- Test: `tests/test_raw_evidence_store.py`

- [ ] **Step 1: Write failing direct-id search test**

Append:

```python
def test_raw_chunk_search_matches_chunk_record_id(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    chunk = runtime.raw.ingest_text(
        text="The user prefers PostgreSQL for durable backups.",
        scope=scope,
        source_event_id="event-2",
        session_id="sess-2",
    )[0]

    results = runtime.store.search(query=chunk.record_id, kinds=["raw_chunk"], scope=scope, limit=5)

    assert results
    assert results[0].record_id == chunk.record_id
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_raw_chunk_search_matches_chunk_record_id -q
```

Expected: no result or wrong top result because `record_id` is not indexed in `content_text`.

- [ ] **Step 3: Extend `content_text`**

In `SqliteRecordStore.upsert`, change the `content_text` parts to include:

```python
record.record_id,
str(record.content.get("text", "")),
str(record.content.get("raw_text", "")),
str(record.content.get("session_id", "")),
str(record.content.get("turn_id", "")),
```

Keep existing fields.

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py::test_raw_chunk_search_matches_chunk_record_id -q
```

Expected: pass.

## Task 5: Add Raw Retrieval And Reranking Helpers

**Files:**
- Create: `eimemory/raw/retrieval.py`
- Test: `tests/test_two_stage_recall.py`

- [ ] **Step 1: Write failing rerank tests**

Create `tests/test_two_stage_recall.py`:

```python
from eimemory.api.runtime import Runtime
from eimemory.raw.retrieval import search_raw_chunks


def test_raw_retrieval_boosts_quoted_phrase_and_proper_noun(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    target = runtime.raw.ingest_text(
        text="Rachel said the phrase sexual compulsions during the ukulele discussion.",
        scope=scope,
        source_event_id="event-target",
        session_id="sess-target",
        speaker="Rachel",
        max_chars=200,
    )[0]
    runtime.raw.ingest_text(
        text="A generic discussion about music therapy and instruments.",
        scope=scope,
        source_event_id="event-noise",
        session_id="sess-noise",
        speaker="Sam",
        max_chars=200,
    )

    results = search_raw_chunks(
        runtime.store,
        query="What did Rachel say about 'sexual compulsions'?",
        scope=scope,
        limit=5,
    )

    assert results[0].record.record_id == target.record_id
    assert "quoted_phrase" in results[0].boosts
    assert "proper_noun" in results[0].boosts
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_two_stage_recall.py::test_raw_retrieval_boosts_quoted_phrase_and_proper_noun -q
```

Expected: import error for `eimemory.raw.retrieval`.

- [ ] **Step 3: Implement deterministic raw retrieval**

Create `eimemory/raw/retrieval.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from eimemory.models.records import RecordEnvelope
from eimemory.storage.runtime_store import RuntimeStore


@dataclass(frozen=True)
class RawSearchResult:
    record: RecordEnvelope
    base_score: float
    final_score: float
    boosts: list[str]


def search_raw_chunks(
    store: RuntimeStore,
    *,
    query: str,
    scope: dict,
    limit: int = 20,
) -> list[RawSearchResult]:
    candidates, report = store.search_with_diagnostics(
        query=query,
        kinds=["raw_chunk"],
        scope=scope,
        limit=max(limit * 4, limit),
        recall_filters={"scoring_profile": "balanced"},
    )
    score_by_id = {
        str(item.get("record_id")): float(item.get("final_score") or 0.0)
        for item in report.get("scored_items") or []
    }
    reranked = [_rerank_candidate(query, record, score_by_id.get(record.record_id, 0.0)) for record in candidates]
    reranked.sort(key=lambda item: item.final_score, reverse=True)
    return reranked[: max(1, int(limit))]


def _rerank_candidate(query: str, record: RecordEnvelope, base_score: float) -> RawSearchResult:
    query_text = str(query or "")
    haystack = " ".join([record.title, record.summary, record.detail, str(record.content.get("text") or "")])
    final = float(base_score)
    boosts: list[str] = []
    for phrase in re.findall(r"'([^']+)'|\"([^\"]+)\"", query_text):
        value = next((part for part in phrase if part), "")
        if value and value.lower() in haystack.lower():
            final += 0.25
            boosts.append("quoted_phrase")
    proper_nouns = re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", query_text)
    for name in proper_nouns:
        if name in haystack:
            final += 0.12
            boosts.append("proper_noun")
            break
    query_terms = {term.lower() for term in re.findall(r"[\w]+", query_text) if len(term) > 2}
    text_terms = {term.lower() for term in re.findall(r"[\w]+", haystack) if len(term) > 2}
    if query_terms:
        final += min(0.2, len(query_terms & text_terms) / len(query_terms) * 0.2)
        if query_terms & text_terms:
            boosts.append("keyword_overlap")
    return RawSearchResult(record=record, base_score=round(base_score, 4), final_score=round(final, 4), boosts=boosts)
```

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_two_stage_recall.py::test_raw_retrieval_boosts_quoted_phrase_and_proper_noun -q
```

Expected: pass.

## Task 6: Implement LongMemEval Adapter

**Files:**
- Create: `eimemory/evaluation/longmemeval.py`
- Modify: `eimemory/evaluation/__init__.py`
- Test: `tests/test_longmemeval_adapter.py`

- [ ] **Step 1: Write failing LongMemEval smoke test**

Create `tests/test_longmemeval_adapter.py`:

```python
from eimemory.api.runtime import Runtime
from eimemory.evaluation.longmemeval import run_longmemeval


def test_longmemeval_adapter_scores_raw_session_retrieval(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "longmem-smoke",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
        "samples": [
            {
                "question_id": "q1",
                "question_type": "single-session preference",
                "question": "Which database does the user prefer?",
                "answer_session_ids": ["sess-b"],
                "haystack_session_ids": ["sess-a", "sess-b"],
                "haystack_dates": ["2026-01-01", "2026-01-02"],
                "haystack_sessions": [
                    [{"role": "user", "content": "I tried SQLite for a toy project."}],
                    [{"role": "user", "content": "I prefer PostgreSQL for durable backups."}],
                ],
            }
        ],
    }

    report = run_longmemeval(runtime, dataset, mode="raw", granularity="session", limit=5)

    assert report["ok"] is True
    assert report["report_type"] == "longmemeval_eval"
    assert report["sample_count"] == 1
    assert report["retrieval"]["recall_any_at_5"] == 1.0
    assert report["samples"][0]["hit_any_at_5"] is True
    assert report["samples"][0]["gold_session_ids"] == ["sess-b"]
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py::test_longmemeval_adapter_scores_raw_session_retrieval -q
```

Expected: import error.

- [ ] **Step 3: Implement `run_longmemeval`**

Create `eimemory/evaluation/longmemeval.py` with:

```python
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.metrics import mean_reciprocal_rank, ndcg_at_k
from eimemory.models.records import ScopeRef
from eimemory.raw.retrieval import search_raw_chunks


def run_longmemeval(
    runtime: Any,
    dataset: dict,
    *,
    mode: str = "raw",
    granularity: str = "session",
    limit: int = 10,
    persist_report: bool = False,
) -> dict[str, Any]:
    suite = _normalize_longmemeval_dataset(dataset)
    scope_ref = ScopeRef.from_dict(suite["scope"])
    samples: list[dict[str, Any]] = []
    ingested_count = 0
    ranks: list[int] = []
    latencies: list[float] = []
    for index, sample in enumerate(suite["samples"]):
        ingested_count += _ingest_sample(runtime, sample, scope=asdict(scope_ref), granularity=granularity)
        started = time.perf_counter()
        results = search_raw_chunks(runtime.store, query=sample["question"], scope=asdict(scope_ref), limit=limit)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        latencies.append(latency_ms)
        returned_session_ids = _dedupe([str(item.record.content.get("session_id") or "") for item in results])
        gold_session_ids = list(sample["answer_session_ids"])
        rank = _first_rank(returned_session_ids, set(gold_session_ids))
        ranks.append(rank)
        sample_report = {
            "index": index,
            "question_id": sample["question_id"],
            "question_type": sample["question_type"],
            "question": sample["question"],
            "gold_session_ids": gold_session_ids,
            "returned_session_ids": returned_session_ids[:limit],
            "returned_chunk_ids": [item.record.record_id for item in results[:limit]],
            "hit_any_at_1": _hit_any(returned_session_ids, gold_session_ids, k=1),
            "hit_any_at_5": _hit_any(returned_session_ids, gold_session_ids, k=5),
            "hit_any_at_10": _hit_any(returned_session_ids, gold_session_ids, k=10),
            "rank": rank,
            "latency_ms": latency_ms,
        }
        samples.append(sample_report)
    report = _build_report(
        suite=suite,
        mode=mode,
        granularity=granularity,
        samples=samples,
        ranks=ranks,
        latencies=latencies,
        ingested_count=ingested_count,
    )
    if persist_report:
        runtime.store.append(_report_record(report, scope_ref))
    return report
```

Then implement the private helpers in the same file:
- `_normalize_longmemeval_dataset`
- `_ingest_sample`
- `_build_report`
- `_hit_any`
- `_first_rank`
- `_dedupe`
- `_report_record`

Keep the helper behavior deterministic:
- Accept `samples` or top-level list.
- Accept `answer_session_ids`, `gold_session_ids`, or `target_session_ids`.
- For session granularity, join all turns with `role: content`.
- For turn granularity, ingest each turn with `turn_id`.

- [ ] **Step 4: Export runner**

Update `eimemory/evaluation/__init__.py`:

```python
from .longmemeval import run_longmemeval

__all__ = ["run_evaluation", "run_memory_eval_ci", "run_longmemeval"]
```

- [ ] **Step 5: Verify green**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py -q
```

Expected: pass.

## Task 7: Add Runtime And CLI Entry

**Files:**
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_longmemeval_adapter.py`
- Test: `tests/test_cli_governance.py`

- [ ] **Step 1: Write failing runtime wrapper test**

Append to `tests/test_longmemeval_adapter.py`:

```python
def test_runtime_exposes_longmemeval_runner(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    report = runtime.run_longmemeval(
        {"name": "empty", "scope": {"agent_id": "hongtu", "workspace_id": "embodied"}, "samples": []},
        mode="raw",
        granularity="session",
        limit=5,
    )
    assert report["ok"] is True
    assert report["sample_count"] == 0
```

- [ ] **Step 2: Write failing CLI test**

Append to `tests/test_cli_governance.py`:

```python
def test_cli_eval_longmem_runs_smoke_dataset(tmp_path, monkeypatch, capsys) -> None:
    from eimemory.cli.main import main as cli_main

    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "longmem.json"
    dataset_path.write_text(
        json.dumps(
            {
                "name": "longmem-cli-smoke",
                "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
                "samples": [
                    {
                        "question_id": "q1",
                        "question_type": "single-session preference",
                        "question": "Which database does the user prefer?",
                        "answer_session_ids": ["sess-b"],
                        "haystack_session_ids": ["sess-a", "sess-b"],
                        "haystack_dates": ["2026-01-01", "2026-01-02"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "I tried SQLite."}],
                            [{"role": "user", "content": "I prefer PostgreSQL."}],
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert cli_main(["eval", "longmem", str(dataset_path), "--mode", "raw", "--granularity", "session"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report_type"] == "longmemeval_eval"
    assert payload["retrieval"]["recall_any_at_5"] == 1.0
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py::test_runtime_exposes_longmemeval_runner tests/test_cli_governance.py::test_cli_eval_longmem_runs_smoke_dataset -q
```

Expected: missing runtime method and CLI subcommand.

- [ ] **Step 4: Add runtime wrapper**

In `Runtime`:

```python
def run_longmemeval(
    self,
    dataset: dict,
    *,
    mode: str = "raw",
    granularity: str = "session",
    limit: int = 10,
    persist_report: bool = False,
) -> dict:
    from eimemory.evaluation.longmemeval import run_longmemeval

    return run_longmemeval(
        self,
        dataset,
        mode=mode,
        granularity=granularity,
        limit=limit,
        persist_report=persist_report,
    )
```

- [ ] **Step 5: Add CLI parser**

In `_build_parser`, add:

```python
eval_longmem = eval_sub.add_parser("longmem")
eval_longmem.add_argument("dataset_json")
eval_longmem.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
eval_longmem.add_argument("--granularity", choices=["session", "turn", "chunk"], default="session")
eval_longmem.add_argument("--limit", type=int, default=10)
eval_longmem.add_argument("--persist-report", action="store_true")
eval_longmem.add_argument("--output", default="")
```

In `main`, add `if parsed.eval_command == "longmem":` mirroring `eval ci` JSON load and output behavior. Return `0` when `report["ok"]` is true.

- [ ] **Step 6: Verify green**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py tests/test_cli_governance.py::test_cli_eval_longmem_runs_smoke_dataset -q
```

Expected: pass.

## Task 8: Add Two-Stage Recall Mode

**Files:**
- Modify: `eimemory/api/memory.py`
- Modify: `eimemory/raw/retrieval.py`
- Test: `tests/test_two_stage_recall.py`

- [ ] **Step 1: Write failing two-stage recall test**

Append:

```python
def test_memory_recall_can_return_raw_evidence_bundle(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    raw = runtime.raw.ingest_text(
        text="The user prefers PostgreSQL because backups are easier.",
        scope=scope,
        source_event_id="event-3",
        session_id="sess-3",
        speaker="user",
        max_chars=200,
    )[0]
    memory = runtime.memory.ingest(
        text="User prefers PostgreSQL for easier backups.",
        memory_type="preference",
        title="Database preference",
        scope=scope,
        links=[],
        force_capture=True,
    )
    memory.links.append({"relation": "derived_from", "target_kind": "raw_chunk", "target_id": raw.record_id})
    runtime.store.rewrite(memory)

    bundle = runtime.memory.recall(
        query="Which database does the user prefer for backups?",
        scope=scope,
        task_context={"recall_mode": "raw_hybrid"},
        limit=5,
    )

    assert bundle.items
    assert bundle.explanation["recall_mode"] == "raw_hybrid"
    assert bundle.explanation["raw_evidence"]["items"][0]["record_id"] == raw.record_id
```

If `LinkRef` objects are required instead of dictionaries, write the link as:

```python
from eimemory.models.records import LinkRef
memory.links.append(LinkRef(relation="derived_from", target_kind="raw_chunk", target_id=raw.record_id))
runtime.store.rewrite(memory)
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_two_stage_recall.py::test_memory_recall_can_return_raw_evidence_bundle -q
```

Expected: no `raw_evidence` explanation.

- [ ] **Step 3: Implement opt-in raw hybrid recall**

In `MemoryAPI.recall`:
- Detect `raw_hybrid = str(task_context.get("recall_mode") or "") == "raw_hybrid"`.
- Before structured search, call `search_raw_chunks(...)`.
- Keep structured search unchanged.
- Add `raw_evidence` to explanation:

```python
"raw_evidence": {
    "count": len(raw_results),
    "items": [
        {
            "record_id": item.record.record_id,
            "session_id": item.record.content.get("session_id", ""),
            "chunk_index": item.record.content.get("chunk_index", 0),
            "summary": item.record.summary,
            "boosts": item.boosts,
            "final_score": item.final_score,
        }
        for item in raw_results[:limit]
    ],
},
"recall_mode": "raw_hybrid" if raw_hybrid else "structured",
```

Do not insert raw chunks into `bundle.items` in `0.3.0`. Keep them in `explanation.raw_evidence` so existing consumers do not suddenly receive unknown item kinds. Any future `include_raw_items` flag must be implemented in a separate release with explicit consumer tests.

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_two_stage_recall.py -q
```

Expected: pass.

## Task 9: Add Preference Synthetic Evidence Without Dropping Raw Text

**Files:**
- Create: `eimemory/raw/synthetic.py`
- Modify: `eimemory/evaluation/longmemeval.py`
- Test: `tests/test_longmemeval_adapter.py`

- [ ] **Step 1: Write failing preference-pattern test**

Append:

```python
def test_longmemeval_hybrid_adds_preference_synthetic_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "preference-smoke",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
        "samples": [
            {
                "question_id": "q-pref",
                "question_type": "single-session preference",
                "question": "What database backend does the user prefer?",
                "answer_session_ids": ["sess-pref"],
                "haystack_session_ids": ["sess-pref"],
                "haystack_dates": ["2026-02-01"],
                "haystack_sessions": [
                    [{"role": "user", "content": "I find Postgres more reliable in my experience."}]
                ],
            }
        ],
    }

    report = runtime.run_longmemeval(dataset, mode="hybrid", granularity="session", limit=5)

    assert report["retrieval"]["recall_any_at_5"] == 1.0
    assert report["samples"][0]["synthetic_evidence_count"] >= 1
```

- [ ] **Step 2: Run failing test**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py::test_longmemeval_hybrid_adds_preference_synthetic_evidence -q
```

Expected: missing synthetic count or no synthetic evidence.

- [ ] **Step 3: Implement synthetic preference patterns**

Create `eimemory/raw/synthetic.py`:

```python
from __future__ import annotations

import re


PREFERENCE_PATTERNS = (
    (re.compile(r"\bI prefer ([^.]+)", re.I), "User has mentioned preference for {value}"),
    (re.compile(r"\bI usually prefer ([^.]+)", re.I), "User has mentioned usual preference for {value}"),
    (re.compile(r"\bI don't like ([^.]+)", re.I), "User has mentioned dislike for {value}"),
    (re.compile(r"\bI find ([^.]+) more reliable", re.I), "User has mentioned finding {value} more reliable"),
)


def synthetic_preference_texts(text: str) -> list[str]:
    results: list[str] = []
    for pattern, template in PREFERENCE_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            value = " ".join(match.group(1).split())[:180]
            if value:
                results.append(template.format(value=value))
    return results
```

In `longmemeval.py`, when `mode == "hybrid"`, ingest synthetic preference texts as `raw_chunk` records with:
- `source_type: "synthetic_preference"`
- `session_id` equal to the source session.
- `meta.synthetic: True`
- `content.source_raw_chunk_id` set to the source raw chunk id.
- `links` containing `LinkRef(relation="derived_from", record_id=source_raw_chunk.record_id, weight=1.0)`.

Do not delete or replace the raw chunk.

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py -q
```

Expected: pass.

## Task 10: Add Temporal And Conflict Scoring Signals

**Files:**
- Modify: `eimemory/raw/retrieval.py`
- Test: `tests/test_two_stage_recall.py`

- [ ] **Step 1: Write failing temporal boost test**

Append:

```python
def test_raw_retrieval_boosts_question_date_proximity(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    old = runtime.raw.ingest_text(
        text="The user lived in Chengdu.",
        scope=scope,
        source_event_id="old",
        session_id="sess-old",
        occurred_at="2026-01-01",
        max_chars=200,
    )[0]
    current = runtime.raw.ingest_text(
        text="The user now lives in Chongqing.",
        scope=scope,
        source_event_id="new",
        session_id="sess-new",
        occurred_at="2026-05-01",
        max_chars=200,
    )[0]

    results = search_raw_chunks(
        runtime.store,
        query="Where does the user live now?",
        scope=scope,
        limit=5,
    )

    assert results[0].record.record_id == current.record_id
```

- [ ] **Step 2: Implement simple currentness boost**

In `_rerank_candidate`, add:
- If query contains `"now"`, `"current"`, `"currently"`, `"现在"`, `"目前"`, boost records with later `occurred_at`.
- Use lexicographic ISO/date strings as the first deterministic version.
- Add `"currentness"` to `boosts`.

- [ ] **Step 3: Verify**

Run:

```bash
python -m pytest tests/test_two_stage_recall.py::test_raw_retrieval_boosts_question_date_proximity -q
```

Expected: pass.

## Task 11: Persist LongMemEval Reports And Surface Governance

**Files:**
- Modify: `eimemory/evaluation/longmemeval.py`
- Modify: `eimemory/governance/snapshot.py`
- Test: `tests/test_longmemeval_adapter.py`
- Test: `tests/test_governance.py`

- [ ] **Step 1: Write failing persisted report test**

Append to `tests/test_longmemeval_adapter.py`:

```python
def test_longmemeval_report_can_be_persisted(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    report = runtime.run_longmemeval(
        {"name": "empty", "scope": {"agent_id": "hongtu", "workspace_id": "embodied"}, "samples": []},
        persist_report=True,
    )

    records = runtime.store.list_records(
        kinds=["reflection"],
        scope={"agent_id": "hongtu", "workspace_id": "embodied"},
        limit=10,
    )
    assert report["persisted"] is True
    assert records[0].meta["report_type"] == "longmemeval_eval"
```

- [ ] **Step 2: Write failing governance test**

Append to `tests/test_governance.py`:

```python
def test_governance_snapshot_surfaces_latest_longmemeval_report(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    runtime.run_longmemeval({"name": "empty-longmem", "scope": scope, "samples": []}, persist_report=True)

    from eimemory.governance.snapshot import build_governance_snapshot

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["longmemeval"]["count"] == 1
    assert snapshot["longmemeval"]["latest"]["name"] == "empty-longmem"
```

- [ ] **Step 3: Implement persistence**

In `longmemeval.py`, `_report_record(report, scope_ref)` should create:

```python
RecordEnvelope.create(
    kind="reflection",
    title=f"LongMemEval report: {report['name']}",
    summary=(
        f"LongMemEval {report['mode']}: "
        f"R@5={report['retrieval']['recall_any_at_5']}, "
        f"NDCG@5={report['retrieval']['ndcg_at_5']}"
    ),
    content={"report": report},
    tags=["longmemeval", "memory-eval"],
    source="eimemory.longmemeval",
    scope=scope_ref,
    meta={
        "report_type": "longmemeval_eval",
        "name": report["name"],
        "mode": report["mode"],
        "recall_any_at_5": report["retrieval"]["recall_any_at_5"],
    },
)
```

Return `persisted` and `persisted_record_id` in the report.

- [ ] **Step 4: Implement governance summary**

In `snapshot.py`:
- Add `_list_longmemeval_report_records`.
- Add `longmemeval` top-level section with `count`, `latest`, and compact metrics.

- [ ] **Step 5: Verify**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py tests/test_governance.py::test_governance_snapshot_surfaces_latest_longmemeval_report -q
```

Expected: pass.

## Task 12: Documentation And Smoke Dataset

**Files:**
- Create: `examples/evaluation/longmemeval_smoke.json`
- Modify: `docs/evaluation.md`
- Test: `tests/test_longmemeval_adapter.py`

- [ ] **Step 1: Add smoke dataset**

Create `examples/evaluation/longmemeval_smoke.json` with two samples:
- One preference query where raw session retrieval should hit.
- One temporal/currentness query where the later session should rank first.

- [ ] **Step 2: Add docs**

In `docs/evaluation.md`, add sections:
- `Raw Evidence Layer`
- `LongMemEval Adapter`
- `Retrieval Recall vs QA Accuracy`
- `Two-stage Recall`

Include commands:

```bash
eimemory eval longmem examples/evaluation/longmemeval_smoke.json --mode raw --granularity session
eimemory eval longmem examples/evaluation/longmemeval_smoke.json --mode hybrid --granularity session --persist-report
```

- [ ] **Step 3: Add example-file test**

Append:

```python
def test_longmemeval_smoke_example_runs(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    with open("examples/evaluation/longmemeval_smoke.json", "r", encoding="utf-8") as handle:
        dataset = json.load(handle)

    report = runtime.run_longmemeval(dataset, mode="raw", granularity="session", limit=5)

    assert report["ok"] is True
    assert report["sample_count"] == 2
    assert report["retrieval"]["recall_any_at_5"] >= 0.5
```

- [ ] **Step 4: Verify docs-adjacent tests**

Run:

```bash
python -m pytest tests/test_longmemeval_adapter.py -q
```

Expected: pass.

## Task 13: Version Bump And Full Regression

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`

- [ ] **Step 1: Update version files**

Set:

```toml
version = "0.3.0"
```

and:

```python
__version__ = "0.3.0"
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
python -m pytest tests/test_raw_evidence_store.py tests/test_two_stage_recall.py tests/test_longmemeval_adapter.py tests/test_version.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full regression**

Run:

```bash
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

Commit once all tests pass:

```bash
git add eimemory/raw eimemory/evaluation eimemory/models/records.py eimemory/api/runtime.py eimemory/api/memory.py eimemory/storage/sqlite_store.py eimemory/cli/main.py eimemory/governance/snapshot.py docs/evaluation.md examples/evaluation/longmemeval_smoke.json tests/test_raw_evidence_store.py tests/test_two_stage_recall.py tests/test_longmemeval_adapter.py tests/test_version.py pyproject.toml eimemory/version.py
git commit -m "feat: add raw evidence LongMemEval recall"
```

## Implementation Order

Use this order exactly:

1. `raw_chunk` record kind.
2. Chunking helpers.
3. RawEvidenceAPI ingestion and context windows.
4. SQLite indexing for raw ids/text.
5. Raw retrieval/rerank helpers.
6. LongMemEval adapter.
7. Runtime and CLI entry.
8. Two-stage recall explanation.
9. Synthetic preference evidence.
10. Temporal/currentness boost.
11. Report persistence and governance.
12. Docs/example.
13. Version bump/full regression.

This order keeps each commit testable and prevents the LongMemEval adapter from being built on an unproven raw evidence layer.

## Review Checklist

Before considering the implementation complete:

- Raw chunks contain verbatim text and can be traced to session/source ids.
- Structured memories are not required for raw LongMemEval retrieval to work.
- LongMemEval reports label metrics as retrieval metrics.
- `mode="raw"` does not create synthetic evidence.
- `mode="hybrid"` never deletes raw chunks.
- Existing preference recall tests still pass.
- Existing rule evolution steady-state tests still pass.
- Full `python -m pytest -q` passes.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-17-eimemory-longmemeval-raw-evidence.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task group, with review between tasks.
2. **Inline Execution** - execute tasks in this session using the plan sequentially.

Recommended split for subagents:
- Agent A: Tasks 1-4, raw evidence store.
- Agent B: Tasks 5 and 8-10, raw retrieval/two-stage recall.
- Agent C: Tasks 6-7 and 11-12, LongMemEval adapter/CLI/governance/docs.
- Parent agent: version bump, integration, full regression, final review.
