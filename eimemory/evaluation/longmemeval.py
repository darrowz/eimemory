"""LongMemEval-style retrieval adapter for raw evidence evaluation."""

from __future__ import annotations

from dataclasses import asdict
import re
from statistics import mean
from time import perf_counter
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.metrics import (
    first_relevant_rank,
    mean_reciprocal_rank,
    ndcg_at_k,
    percentile,
    recall_all_at_k,
    recall_any_at_k,
    recall_at_k,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


def normalize_longmemeval_dataset(dataset: dict | list) -> dict[str, Any]:
    raw = {"name": "longmemeval", "cases": dataset} if isinstance(dataset, list) else dict(dataset)
    if not isinstance(raw, dict):
        raise ValueError("LongMemEval dataset must be a JSON object or list")
    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    cases = [
        _normalize_case(item, index=index, default_scope=scope)
        for index, item in enumerate(list(raw.get("cases") or raw.get("samples") or raw.get("data") or []))
        if isinstance(item, dict)
    ]
    return {
        "schema_version": 1,
        "name": str(raw.get("name") or raw.get("dataset_name") or "longmemeval"),
        "scope": scope,
        "cases": cases,
    }


def run_longmemeval(
    runtime,
    dataset: dict | list,
    *,
    mode: str = "raw",
    granularity: str = "session",
    limit: int = 10,
    persist_report: bool = False,
) -> dict[str, Any]:
    normalized = normalize_longmemeval_dataset(dataset)
    mode = _normalize_choice(mode, allowed={"raw", "hybrid"}, default="raw")
    granularity = _normalize_choice(granularity, allowed={"session", "turn", "chunk"}, default="session")
    limit = max(1, min(1000, int(limit)))
    dataset_scope = ScopeRef.from_dict(normalized["scope"])

    samples: list[dict[str, Any]] = []
    ranks: list[int] = []
    latencies: list[float] = []
    by_type_samples: dict[str, list[dict[str, Any]]] = {}

    for index, case in enumerate(normalized["cases"]):
        case_scope = ScopeRef.from_dict(case.get("scope") or normalized["scope"])
        _ingest_case_chunks(runtime, case=case, scope=case_scope)
        start = perf_counter()
        retrieved = _retrieve(
            runtime,
            query=case["question"],
            scope=case_scope,
            mode=mode,
            limit=limit,
            benchmark_case_id=case["case_id"],
        )
        latency_ms = (perf_counter() - start) * 1000.0
        latencies.append(latency_ms)

        returned_ids = _returned_ids(retrieved, granularity=granularity, query=case["question"])
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
            "retrieval_recall_at_1": recall_at_k(returned_ids, expected_ids, k=1),
            "retrieval_recall_at_5": recall_at_k(returned_ids, expected_ids, k=5),
            "retrieval_recall_at_10": recall_at_k(returned_ids, expected_ids, k=10),
            "recall_any_at_1": recall_any_at_k(returned_ids, expected_ids, k=1),
            "recall_any_at_5": recall_any_at_k(returned_ids, expected_ids, k=5),
            "recall_any_at_10": recall_any_at_k(returned_ids, expected_ids, k=10),
            "recall_all_at_1": recall_all_at_k(returned_ids, expected_ids, k=1),
            "recall_all_at_5": recall_all_at_k(returned_ids, expected_ids, k=5),
            "recall_all_at_10": recall_all_at_k(returned_ids, expected_ids, k=10),
            "ndcg_at_5": ndcg_at_k(returned_ids, expected_ids, k=5),
            "rank": rank,
            "reciprocal_rank": round((1.0 / rank) if rank else 0.0, 3),
            "latency_ms": round(latency_ms, 3),
        }
        samples.append(sample)
        by_type_samples.setdefault(case["question_type"] or "unknown", []).append(sample)

    report = {
        "ok": True,
        "schema_version": 1,
        "report_type": "longmemeval_eval",
        "name": normalized["name"],
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "mode": mode,
        "granularity": granularity,
        "limit": limit,
        "sample_count": len(samples),
        "retrieval_recall_at_1": _avg(samples, "retrieval_recall_at_1"),
        "retrieval_recall_at_5": _avg(samples, "retrieval_recall_at_5"),
        "retrieval_recall_at_10": _avg(samples, "retrieval_recall_at_10"),
        "recall_any_at_1": _avg(samples, "recall_any_at_1"),
        "recall_any_at_5": _avg(samples, "recall_any_at_5"),
        "recall_any_at_10": _avg(samples, "recall_any_at_10"),
        "recall_all_at_1": _avg(samples, "recall_all_at_1"),
        "recall_all_at_5": _avg(samples, "recall_all_at_5"),
        "recall_all_at_10": _avg(samples, "recall_all_at_10"),
        "ndcg_at_5": _avg(samples, "ndcg_at_5"),
        "mrr": mean_reciprocal_rank(ranks),
        "latency_ms_avg": round(mean(latencies), 3) if latencies else 0.0,
        "latency_ms_p95": percentile(latencies, 95),
        "by_question_type": {
            question_type: _summarize_samples(type_samples)
            for question_type, type_samples in sorted(by_type_samples.items())
        },
        "samples": samples,
        "persisted": False,
        "persisted_record_id": "",
    }
    if persist_report:
        record = _report_record(report, scope=dataset_scope)
        runtime.store.append(record)
        report = {**report, "persisted": True, "persisted_record_id": record.record_id}
    return report


def _normalize_case(case: dict[str, Any], *, index: int, default_scope: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case.get("question_id") or case.get("id") or case.get("case_id") or f"case-{index + 1}")
    if _is_raw_longmemeval_case(case):
        sessions, mined_session_ids, mined_turn_ids = _normalize_raw_case_haystack(case=case, case_id=case_id)
    else:
        sessions = list(case.get("haystack_sessions") or case.get("sessions") or case.get("haystack") or [])
        mined_session_ids, mined_turn_ids = [], []

    evidence_session_ids = _merge_case_ids(
        case.get("evidence_session_ids"),
        case.get("evidence_sessions"),
        case.get("answer_session_ids"),
        mined_session_ids,
    )
    evidence_turn_ids = _merge_case_ids(
        case.get("evidence_turn_ids"),
        case.get("evidence_turns"),
        mined_turn_ids,
    )

    chunks = _session_chunks(sessions, case_id=case_id)
    return {
        "case_id": case_id,
        "question": str(case.get("question") or case.get("query") or ""),
        "question_type": str(case.get("question_type") or case.get("type") or "unknown"),
        "expected_answer": str(case.get("expected_answer") or case.get("answer") or ""),
        "scope": asdict(ScopeRef.from_dict(case.get("scope") or default_scope)),
        "chunks": chunks,
        "evidence_session_ids": evidence_session_ids,
        "evidence_turn_ids": evidence_turn_ids,
        "evidence_chunk_ids": _strings(case.get("evidence_chunk_ids") or case.get("evidence_chunks")),
    }


def _is_raw_longmemeval_case(case: dict[str, Any]) -> bool:
    sessions = list(case.get("haystack_sessions") or [])
    if not sessions:
        return False
    first = sessions[0]
    return isinstance(first, list)


def _normalize_raw_case_haystack(case: dict[str, Any], *, case_id: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    raw_sessions = list(case.get("haystack_sessions") or [])
    raw_session_ids = list(case.get("haystack_session_ids") or [])
    sessions: list[dict[str, Any]] = []
    evidence_session_ids: list[str] = []
    evidence_turn_ids: list[str] = []

    for session_index, session_messages in enumerate(raw_sessions):
        if not isinstance(session_messages, list):
            continue

        session_id = str(
            raw_session_ids[session_index]
            if session_index < len(raw_session_ids)
            else f"{case_id}-session-{session_index + 1}"
        )
        if not session_id:
            continue

        turns: list[dict[str, Any]] = []
        for message_index, message in enumerate(session_messages):
            if not isinstance(message, dict):
                continue
            content = str(message.get("content") or message.get("text") or message.get("message") or "").strip()
            if not content:
                continue
            turn_id = f"{session_id}:m{message_index}"
            speaker = str(message.get("role") or message.get("speaker") or "").strip()
            turns.append({"turn_id": turn_id, "messages": [{"role": speaker, "content": content}]})

            if message.get("has_answer"):
                evidence_turn_ids = _append_unique(evidence_turn_ids, turn_id)
                evidence_session_ids = _append_unique(evidence_session_ids, session_id)

        if turns:
            sessions.append({"session_id": session_id, "turns": turns})

    return sessions, evidence_session_ids, evidence_turn_ids


def _merge_case_ids(*items: Any) -> list[str]:
    values: list[str] = []
    for item in items:
        for value in _strings(item):
            values = _append_unique(values, value)
    return values


def _append_unique(values: list[str], value: str) -> list[str]:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)
    return values


def _session_chunks(sessions: list[Any], *, case_id: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for session_index, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("session_id") or session.get("id") or f"{case_id}-session-{session_index + 1}")
        texts: list[str] = []
        turn_ids: list[str] = []
        turn_texts: list[dict[str, str]] = []
        for turn_index, turn in enumerate(_turns(session)):
            turn_id = str(turn.get("turn_id") or turn.get("id") or f"{session_id}-turn-{turn_index + 1}")
            turn_text = _messages_text(turn.get("messages") or turn.get("turns") or [turn])
            if turn_text:
                turn_ids.append(turn_id)
                turn_texts.append({"turn_id": turn_id, "text": turn_text})
                texts.append(turn_text)
        if not texts:
            texts.append(_messages_text(session.get("messages") or [session]))
        text = "\n".join(item for item in texts if item.strip()).strip()
        if not text:
            continue
        chunks.append(
            {
                "chunk_id": str(session.get("chunk_id") or f"{case_id}:{session_id}:0"),
                "session_id": session_id,
                "turn_id": turn_ids[0] if turn_ids else "",
                "turn_ids": turn_ids,
                "turn_texts": turn_texts,
                "text": text,
            }
        )
    return chunks


def _turns(session: dict[str, Any]) -> list[dict[str, Any]]:
    turns = session.get("turns")
    if isinstance(turns, list):
        return [turn for turn in turns if isinstance(turn, dict)]
    return []


def _messages_text(messages: Any) -> str:
    texts: list[str] = []
    for message in list(messages or []):
        if isinstance(message, dict):
            value = message.get("content", message.get("text", message.get("message", "")))
            role = str(message.get("role") or message.get("speaker") or "").strip()
            text = str(value or "").strip()
            if text:
                texts.append(f"{role}: {text}" if role else text)
        elif str(message or "").strip():
            texts.append(str(message).strip())
    return "\n".join(texts)


def _ingest_case_chunks(runtime, *, case: dict[str, Any], scope: ScopeRef) -> None:
    for chunk in case["chunks"]:
        if _existing_raw_chunk(runtime, chunk["chunk_id"], scope=scope):
            continue
        if not _ingest_with_raw_api(runtime, chunk=chunk, case=case, scope=scope):
            runtime.store.append(_raw_chunk_record(chunk, case=case, scope=scope))
        _mark_raw_chunk_ingested(runtime, chunk["chunk_id"], scope=scope)


def _ingest_with_raw_api(runtime, *, chunk: dict[str, Any], case: dict[str, Any], scope: ScopeRef) -> bool:
    payload = {
        "text": chunk["text"],
        "raw_text": chunk["text"],
        "chunk_id": chunk["chunk_id"],
        "session_id": chunk["session_id"],
        "turn_id": chunk["turn_id"],
        "turn_ids": list(chunk["turn_ids"]),
        "turn_texts": list(chunk.get("turn_texts") or []),
        "source": "eimemory.longmemeval",
        "meta": {"longmemeval_case_id": case["case_id"], "benchmark_case_id": case["case_id"]},
    }
    candidates = [getattr(runtime, "raw", None)]
    try:
        from eimemory.raw.store import RawEvidenceAPI  # type: ignore

        candidates.append(RawEvidenceAPI(runtime.store))
    except Exception:
        pass
    for api in [candidate for candidate in candidates if candidate is not None]:
        for method_name in ("ingest_chunk", "append_chunk", "ingest"):
            method = getattr(api, method_name, None)
            if not callable(method):
                continue
            try:
                method(payload, scope=scope)
                return True
            except TypeError:
                try:
                    kwargs = dict(payload)
                    kwargs.pop("text", None)
                    method(text=chunk["text"], scope=scope, **kwargs)
                    return True
                except Exception:
                    continue
            except Exception:
                continue
    return False


def _raw_chunk_record(chunk: dict[str, Any], *, case: dict[str, Any], scope: ScopeRef) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="raw_chunk",
        title=f"LongMemEval raw chunk {chunk['session_id']}",
        summary=chunk["text"][:240],
        detail=chunk["text"],
        content={
            "text": chunk["text"],
            "raw_text": chunk["text"],
            "chunk_id": chunk["chunk_id"],
            "session_id": chunk["session_id"],
            "turn_id": chunk["turn_id"],
            "turn_ids": list(chunk["turn_ids"]),
            "turn_texts": list(chunk.get("turn_texts") or []),
        },
        scope=scope,
        source="eimemory.longmemeval",
        meta={"memory_type": "raw_chunk", "longmemeval_case_id": case["case_id"], "benchmark_case_id": case["case_id"]},
    )


def _existing_raw_chunk(runtime, chunk_id: str, *, scope: ScopeRef) -> bool:
    cache_key = _raw_chunk_cache_key(chunk_id, scope=scope)
    cache = getattr(runtime, "_eimemory_eval_raw_chunk_ids", None)
    if cache is None:
        cache = set()
        setattr(runtime, "_eimemory_eval_raw_chunk_ids", cache)
    if cache_key in cache:
        return True
    for record in runtime.store.list_records(kinds=["raw_chunk"], scope=scope, limit=1000):
        if record.content.get("chunk_id") == chunk_id:
            cache.add(cache_key)
            return True
    return False


def _mark_raw_chunk_ingested(runtime, chunk_id: str, *, scope: ScopeRef) -> None:
    cache = getattr(runtime, "_eimemory_eval_raw_chunk_ids", None)
    if cache is None:
        cache = set()
        setattr(runtime, "_eimemory_eval_raw_chunk_ids", cache)
    cache.add(_raw_chunk_cache_key(chunk_id, scope=scope))


def _raw_chunk_cache_key(chunk_id: str, *, scope: ScopeRef) -> tuple[str, str, str, str, str]:
    return (
        str(scope.tenant_id or "default"),
        str(scope.agent_id or ""),
        str(scope.workspace_id or ""),
        str(scope.user_id or ""),
        str(chunk_id or ""),
    )


def _retrieve(
    runtime,
    *,
    query: str,
    scope: ScopeRef,
    mode: str,
    limit: int,
    benchmark_case_id: str | None = None,
    task_context: dict[str, Any] | None = None,
) -> list[RecordEnvelope]:
    retrieval_context = {"task_type": "longmemeval"}
    retrieval_context.update(dict(task_context or {}))
    if benchmark_case_id:
        retrieval_context["benchmark_case_id"] = str(benchmark_case_id)
    if mode == "hybrid":
        bundle = runtime.memory.recall(
            query=query,
            scope=asdict(scope),
            task_context={**retrieval_context, "recall_mode": "raw_hybrid"},
            limit=limit,
        )
        raw_records = _raw_records_from_explanation(
            runtime,
            bundle.explanation.get("raw_evidence"),
            scope=scope,
            benchmark_case_id=benchmark_case_id,
        )
        if raw_records:
            return raw_records[:limit]
    try:
        from eimemory.raw.retrieval import search_raw_chunks

        ranked = search_raw_chunks(runtime.store, query=query, scope=scope, task_context=retrieval_context, limit=limit)
        records = [
            record
            for item in ranked
            if (record := _record_from_ranked(runtime, item, scope=scope)) is not None
            and _record_matches_case(record, benchmark_case_id=benchmark_case_id)
        ]
        if records:
            return records[:limit]
    except Exception:
        pass
    records = runtime.store.search(query=query, kinds=["raw_chunk"], scope=scope, limit=limit)
    if benchmark_case_id:
        records = _filter_records_by_case(records, benchmark_case_id=benchmark_case_id)
    if records:
        return records[:limit]
    return _case_records_from_store(
        runtime,
        query=query,
        scope=scope,
        limit=limit,
        benchmark_case_id=benchmark_case_id,
        task_context=retrieval_context,
    )


def _raw_records_from_explanation(
    runtime,
    raw_evidence: Any,
    *,
    scope: ScopeRef,
    benchmark_case_id: str | None = None,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    seen: set[str] = set()
    for item in list(raw_evidence or []):
        if not isinstance(item, dict):
            continue
        payload = item.get("record")
        record_id = str(payload.get("record_id") or "") if isinstance(payload, dict) else ""
        if not record_id or record_id in seen:
            continue
        record = runtime.store.get_by_id(record_id, scope=scope)
        if record is None:
            continue
        if not _record_matches_case(record, benchmark_case_id=benchmark_case_id):
            continue
        seen.add(record_id)
        records.append(record)
    return records


def _record_from_ranked(runtime, item: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope | None:
    payload = item.get("record") if isinstance(item, dict) else None
    record_id = str(payload.get("record_id") or "") if isinstance(payload, dict) else ""
    return runtime.store.get_by_id(record_id, scope=scope) if record_id else None


def _case_records_from_store(
    runtime,
    *,
    query: str,
    scope: ScopeRef,
    limit: int,
    benchmark_case_id: str | None,
    task_context: dict[str, Any],
) -> list[RecordEnvelope]:
    if not benchmark_case_id:
        return []
    try:
        records = runtime.store.list_records(kinds=["raw_chunk"], scope=scope, status="active", limit=5000)
    except TypeError:
        try:
            records = runtime.store.list_records(kinds=["raw_chunk"], scope=scope, limit=5000)
        except Exception:
            return []
    except Exception:
        return []

    case_records = _filter_records_by_case(list(records), benchmark_case_id=benchmark_case_id)
    if not case_records:
        return []
    try:
        from eimemory.raw.retrieval import rerank_raw_results

        ranked = rerank_raw_results(query=query, results=case_records, task_context=task_context)
        ordered = [
            record
            for item in ranked
            if (record := _record_from_ranked(runtime, item, scope=scope)) is not None
            and _record_matches_case(record, benchmark_case_id=benchmark_case_id)
        ]
        if ordered:
            return ordered[:limit]
    except Exception:
        pass
    return case_records[:limit]


def _returned_ids(records: list[RecordEnvelope], *, granularity: str, query: str = "") -> list[str]:
    key = {"session": "session_id", "turn": "turn_id", "chunk": "chunk_id"}[granularity]
    values: list[str] = []
    for record in records:
        if granularity == "turn":
            for turn_id in _localized_turn_ids(record, query=query):
                value = str(turn_id or "")
                if value and value not in values:
                    values.append(value)
        value = record.content.get(key)
        if value and str(value) not in values:
            values.append(str(value))
    return values


def _localized_turn_ids(record: RecordEnvelope, *, query: str) -> list[str]:
    turn_ids = [str(item) for item in list(record.content.get("turn_ids") or []) if str(item)]
    if not turn_ids:
        turn_id = str(record.content.get("turn_id") or "")
        return [turn_id] if turn_id else []
    turn_texts = _turn_text_map(record)
    query_terms = set(_terms(query))
    if not turn_texts or not query_terms:
        return turn_ids
    ranked: list[tuple[float, int, str]] = []
    for index, turn_id in enumerate(turn_ids):
        text = turn_texts.get(turn_id, "")
        ranked.append((_turn_local_score(query_terms, text), index, turn_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [turn_id for _score, _index, turn_id in ranked]


def _turn_text_map(record: RecordEnvelope) -> dict[str, str]:
    mapped: dict[str, str] = {}
    raw = record.content.get("turn_texts")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            turn_id = str(item.get("turn_id") or "").strip()
            text = str(item.get("text") or "").strip()
            if turn_id and text:
                mapped[turn_id] = text
    legacy = record.content.get("turn_text_by_id")
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            turn_id = str(key or "").strip()
            text = str(value or "").strip()
            if turn_id and text:
                mapped.setdefault(turn_id, text)
    return mapped


def _turn_local_score(query_terms: set[str], text: str) -> float:
    text_terms = set(_terms(text))
    if not query_terms or not text_terms:
        return 0.0
    overlap = len(query_terms & text_terms) / max(1, len(query_terms))
    text_lower = str(text or "").lower()
    preference_boost = 0.15 if any(marker in text_lower for marker in ("i prefer", "prefer ", "preference")) else 0.0
    return round(overlap + preference_boost, 6)


def _expected_ids(case: dict[str, Any], *, granularity: str) -> set[str]:
    key = {
        "session": "evidence_session_ids",
        "turn": "evidence_turn_ids",
        "chunk": "evidence_chunk_ids",
    }[granularity]
    expected = {str(item) for item in case[key] if str(item)}
    if expected:
        return expected
    if granularity == "chunk":
        return {str(chunk["chunk_id"]) for chunk in case["chunks"] if chunk["session_id"] in set(case["evidence_session_ids"])}
    return set(case["evidence_session_ids"])


def _hit_ids(records: list[RecordEnvelope], *, expected_ids: list[str], key: str) -> list[str]:
    expected = {str(item) for item in expected_ids if str(item)}
    hits: list[str] = []
    for record in records:
        if key == "turn_id":
            for turn_id in list(record.content.get("turn_ids") or []):
                value = str(turn_id or "")
                if value and value in expected and value not in hits:
                    hits.append(value)
        value = str(record.content.get(key) or "")
        if value and value in expected and value not in hits:
            hits.append(value)
    return hits


def _record_case_id(record: RecordEnvelope) -> str:
    meta = record.meta if isinstance(record.meta, dict) else {}
    for key in ("benchmark_case_id", "longmemeval_case_id", "case_id"):
        value = meta.get(key)
        if value:
            return str(value)
    for key in ("benchmark_case_id", "longmemeval_case_id", "case_id"):
        value = record.content.get(key)
        if value:
            return str(value)
    return ""


def _record_matches_case(record: RecordEnvelope, *, benchmark_case_id: str | None) -> bool:
    if not benchmark_case_id:
        return True
    return _record_case_id(record) == str(benchmark_case_id)


def _filter_records_by_case(records: list[RecordEnvelope], *, benchmark_case_id: str | None) -> list[RecordEnvelope]:
    if not benchmark_case_id:
        return records
    return [record for record in records if _record_matches_case(record, benchmark_case_id=benchmark_case_id)]


def _summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sample_count": len(samples),
        "retrieval_recall_at_1": _avg(samples, "retrieval_recall_at_1"),
        "retrieval_recall_at_5": _avg(samples, "retrieval_recall_at_5"),
        "retrieval_recall_at_10": _avg(samples, "retrieval_recall_at_10"),
        "recall_any_at_5": _avg(samples, "recall_any_at_5"),
        "recall_all_at_5": _avg(samples, "recall_all_at_5"),
        "ndcg_at_5": _avg(samples, "ndcg_at_5"),
        "mrr": mean_reciprocal_rank([int(sample["rank"]) for sample in samples]),
    }


def _report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="reflection",
        title=f"LongMemEval report: {report['name']}",
        summary=f"LongMemEval retrieval recall@5={report['retrieval_recall_at_5']}",
        scope=scope,
        source="eimemory.longmemeval",
        content={"report": dict(report)},
        meta={
            "report_type": "longmemeval_eval",
            "name": report["name"],
            "retrieval_recall_at_5": report["retrieval_recall_at_5"],
        },
    )


def _avg(samples: list[dict[str, Any]], key: str) -> float:
    if not samples:
        return 0.0
    return round(sum(float(sample.get(key) or 0.0) for sample in samples) / len(samples), 3)


def _normalize_choice(value: str, *, allowed: set[str], default: str) -> str:
    normalized = str(value or default).strip().lower()
    return normalized if normalized in allowed else default


def _strings(value: Any) -> list[str]:
    return [str(item) for item in list(value or []) if str(item)]


def _terms(text: str) -> list[str]:
    return [
        term.lower()
        for term in re.findall(r"[\w']+", str(text or ""), flags=re.UNICODE)
        if len(term.strip("'")) > 1
    ]
