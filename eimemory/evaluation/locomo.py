"""LoCoMo-style retrieval adapter for long conversation memory benchmarks."""

from __future__ import annotations

from dataclasses import asdict
from statistics import mean
from time import perf_counter
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation._text import extract_text_from_turn
from eimemory.evaluation.longmemeval import (
    _hit_ids,
    _ingest_case_chunks,
    _retrieve,
    _strings,
)
from eimemory.evaluation.metrics import (
    first_relevant_rank,
    mean_reciprocal_rank,
    ndcg_at_k,
    percentile,
    recall_any_at_k,
    recall_at_k,
)
from eimemory.models.records import ScopeRef


def normalize_locomo_dataset(dataset: dict | list) -> dict[str, Any]:
    raw = {"name": "locomo", "cases": dataset} if isinstance(dataset, list) else dict(dataset)
    if not isinstance(raw, dict):
        raise ValueError("LoCoMo dataset must be a JSON object or list")
    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    cases = [
        _normalize_case(item, index=index, default_scope=scope)
        for index, item in enumerate(list(raw.get("cases") or raw.get("samples") or raw.get("data") or []))
        if isinstance(item, dict)
    ]
    return {
        "schema_version": 1,
        "name": str(raw.get("name") or raw.get("dataset_name") or "locomo"),
        "scope": scope,
        "cases": cases,
    }


def run_locomo(
    runtime,
    dataset: dict | list,
    *,
    mode: str = "raw",
    granularity: str = "turn",
    limit: int = 10,
) -> dict[str, Any]:
    normalized = normalize_locomo_dataset(dataset)
    mode = str(mode or "raw").strip().lower()
    if mode not in {"raw", "hybrid"}:
        mode = "raw"
    granularity = str(granularity or "turn").strip().lower()
    if granularity not in {"session", "turn", "chunk"}:
        granularity = "turn"
    limit = max(1, min(1000, int(limit)))
    dataset_scope = ScopeRef.from_dict(normalized["scope"])

    samples: list[dict[str, Any]] = []
    ranks: list[int] = []
    latencies: list[float] = []
    for index, case in enumerate(normalized["cases"]):
        case_scope = ScopeRef.from_dict(case.get("scope") or normalized["scope"])
        _ingest_case_chunks(runtime, case=case, scope=case_scope)
        started = perf_counter()
        retrieved = _retrieve(
            runtime,
            query=case["question"],
            scope=case_scope,
            mode=mode,
            limit=limit,
            task_context={"task_type": "locomo", "granularity": granularity},
        )
        latency_ms = (perf_counter() - started) * 1000.0
        latencies.append(latency_ms)

        returned_ids = _returned_ids(retrieved, granularity=granularity)
        expected_ids = _expected_ids(case, granularity=granularity)
        rank = first_relevant_rank(returned_ids, expected_ids)
        ranks.append(rank)
        sample = {
            "index": index,
            "case_id": case["case_id"],
            "question": case["question"],
            "question_type": case["question_type"],
            "scope": asdict(case_scope),
            "granularity": granularity,
            "expected_ids": sorted(expected_ids),
            "returned_ids": returned_ids,
            "hit_session_ids": _hit_ids(retrieved, expected_ids=case["evidence_session_ids"], key="session_id"),
            "hit_turn_ids": _hit_ids(retrieved, expected_ids=case["evidence_turn_ids"], key="turn_id"),
            "hit_chunk_ids": _hit_ids(retrieved, expected_ids=case["evidence_chunk_ids"], key="chunk_id"),
            "recall_at_1": recall_at_k(returned_ids, expected_ids, k=1),
            "recall_at_5": recall_at_k(returned_ids, expected_ids, k=5),
            "recall_at_10": recall_at_k(returned_ids, expected_ids, k=10),
            "recall_any_at_5": recall_any_at_k(returned_ids, expected_ids, k=5),
            "ndcg_at_5": ndcg_at_k(returned_ids, expected_ids, k=5),
            "rank": rank,
            "reciprocal_rank": round((1.0 / rank) if rank else 0.0, 3),
            "latency_ms": round(latency_ms, 3),
        }
        samples.append(sample)

    failures = [sample for sample in samples if not sample["rank"]]
    return {
        "ok": True,
        "schema_version": 1,
        "report_type": "locomo_eval",
        "name": normalized["name"],
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "mode": mode,
        "granularity": granularity,
        "limit": limit,
        "sample_count": len(samples),
        "recall_at_1": _avg(samples, "recall_at_1"),
        "recall_at_5": _avg(samples, "recall_at_5"),
        "recall_at_10": _avg(samples, "recall_at_10"),
        "mrr": mean_reciprocal_rank(ranks),
        "ndcg_at_5": _avg(samples, "ndcg_at_5"),
        "latency_ms_avg": round(mean(latencies), 3) if latencies else 0.0,
        "latency_ms_p95": percentile(latencies, 95),
        "failure_count": len(failures),
        "failure_samples": failures[:20],
        "samples": samples,
    }


def _normalize_case(case: dict[str, Any], *, index: int, default_scope: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case.get("id") or case.get("case_id") or f"locomo-{index + 1}")
    chunks = _existing_chunks(case)
    if not chunks:
        sessions = _sessions_from_case(case, case_id=case_id)
        chunks = _session_chunks(sessions, case_id=case_id)
    return {
        "case_id": case_id,
        "question": str(case.get("question") or case.get("query") or case.get("qa", {}).get("question") or ""),
        "question_type": str(case.get("question_type") or case.get("type") or case.get("category") or "unknown"),
        "expected_answer": str(case.get("answer") or case.get("expected_answer") or case.get("qa", {}).get("answer") or ""),
        "scope": asdict(ScopeRef.from_dict(case.get("scope") or default_scope)),
        "chunks": chunks,
        "evidence_session_ids": _strings(case.get("evidence_session_ids") or case.get("evidence_sessions")),
        "evidence_turn_ids": _strings(case.get("evidence_turn_ids") or case.get("evidence_turns")),
        "evidence_chunk_ids": _strings(case.get("evidence_chunk_ids") or case.get("evidence_chunks")),
    }


def _existing_chunks(case: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(list(case.get("chunks") or [])):
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or chunk.get("raw_text") or "")
        if not text:
            continue
        turn_id = str(chunk.get("turn_id") or "")
        turn_ids = _strings(chunk.get("turn_ids") or ([turn_id] if turn_id else []))
        chunks.append(
            {
                "chunk_id": str(chunk.get("chunk_id") or f"{case.get('case_id') or case.get('id') or 'locomo'}:chunk:{index}"),
                "session_id": str(chunk.get("session_id") or ""),
                "turn_id": turn_id or (turn_ids[0] if turn_ids else ""),
                "turn_ids": turn_ids,
                "text": text,
            }
        )
    return chunks


def _sessions_from_case(case: dict[str, Any], *, case_id: str) -> list[dict[str, Any]]:
    sessions = case.get("sessions") or case.get("haystack_sessions") or case.get("conversation_sessions")
    if isinstance(sessions, list) and sessions:
        return [item for item in sessions if isinstance(item, dict)]
    conversation = case.get("conversation") or case.get("dialogue") or case.get("messages") or case.get("turns")
    if isinstance(conversation, list):
        return [{"session_id": str(case.get("session_id") or f"{case_id}-session-1"), "turns": conversation}]
    return []


def _session_chunks(sessions: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for session_index, session in enumerate(sessions):
        session_id = str(session.get("session_id") or session.get("id") or f"{case_id}-session-{session_index + 1}")
        for turn_index, turn in enumerate(_turns(session)):
            turn_id = str(turn.get("turn_id") or turn.get("id") or f"{session_id}-turn-{turn_index + 1}")
            text = _turn_text(turn)
            if not text:
                continue
            chunks.append(
                {
                    "chunk_id": str(turn.get("chunk_id") or f"{case_id}:{session_id}:{turn_index}"),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "turn_ids": [turn_id],
                    "text": text,
                }
            )
    return chunks


def _turns(session: dict[str, Any]) -> list[dict[str, Any]]:
    turns = session.get("turns") or session.get("messages") or session.get("dialogue")
    if isinstance(turns, list):
        return [turn if isinstance(turn, dict) else {"text": str(turn)} for turn in turns]
    return []


def _turn_text(turn: dict[str, Any]) -> str:
    # Delegates to the shared helper so that both the flat
    # ``{"speaker": ..., "text": ...}`` shape and the nested
    # ``{"messages": [{"role": ..., "content": ...}, ...]}`` shape
    # produced by the LoCoMo converter produce non-empty text. The
    # previous inline implementation only knew about the flat shape and
    # silently dropped every chunk from the converter output, which is
    # why the LoCoMo R@5 score was exactly 0.0.
    return extract_text_from_turn(turn)


def _returned_ids(records: list[Any], *, granularity: str) -> list[str]:
    key = {"session": "session_id", "turn": "turn_id", "chunk": "chunk_id"}[granularity]
    values: list[str] = []
    for record in records:
        if granularity == "turn":
            for turn_id in list(record.content.get("turn_ids") or []):
                value = str(turn_id or "")
                if value and value not in values:
                    values.append(value)
        value = record.content.get(key)
        if value and str(value) not in values:
            values.append(str(value))
    return values


def _expected_ids(case: dict[str, Any], *, granularity: str) -> set[str]:
    key = {"session": "evidence_session_ids", "turn": "evidence_turn_ids", "chunk": "evidence_chunk_ids"}[granularity]
    expected = {str(item) for item in case[key] if str(item)}
    if expected:
        return expected
    if granularity == "session":
        return {chunk["session_id"] for chunk in case["chunks"]}
    if granularity == "turn":
        return {chunk["turn_id"] for chunk in case["chunks"]}
    return {chunk["chunk_id"] for chunk in case["chunks"]}


def _avg(samples: list[dict[str, Any]], key: str) -> float:
    if not samples:
        return 0.0
    return round(sum(float(sample.get(key) or 0.0) for sample in samples) / len(samples), 3)
