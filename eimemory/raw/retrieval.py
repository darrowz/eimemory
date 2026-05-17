from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.synthetic import synthetic_preference_texts


def search_raw_chunks(
    store: Any,
    *,
    query: str,
    scope: ScopeRef | dict | None = None,
    task_context: dict | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Retrieve raw-ish chunks through Agent A APIs when present, otherwise store search."""
    normalized_query = str(query or "").strip()
    if not normalized_query or limit <= 0:
        return []
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    candidates = _raw_api_search(store, query=normalized_query, scope=scope_ref, limit=max(limit * 4, limit))
    if not candidates:
        candidates = _store_raw_candidates(store, query=normalized_query, scope=scope_ref, limit=max(limit * 6, limit))
    return rerank_raw_results(
        query=normalized_query,
        results=candidates,
        task_context=task_context or {},
    )[:limit]


def rerank_raw_results(
    *,
    query: str,
    results: list[Any],
    task_context: dict | None = None,
) -> list[dict[str, Any]]:
    query_text = str(query or "")
    task_context = dict(task_context or {})
    max_time = _max_timestamp(results)
    ranked: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        record = _result_record(result)
        base_score = _result_base_score(result)
        record_text = _record_text(record)
        boosts = _boosts(
            query=query_text,
            text=record_text,
            record=record,
            task_context=task_context,
            max_time=max_time,
        )
        final_score = round(base_score + sum(boosts.values()), 6)
        ranked.append(
            {
                "record": _record_payload(record, text=record_text),
                "base_score": round(base_score, 6),
                "final_score": final_score,
                "boosts": boosts,
                "_index": index,
            }
        )
    ranked.sort(key=lambda item: (-float(item["final_score"]), item["_index"]))
    for item in ranked:
        item.pop("_index", None)
    return ranked


def _raw_api_search(store: Any, *, query: str, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    try:
        from eimemory.raw.store import RawEvidenceAPI  # type: ignore
    except Exception:
        return []
    try:
        api = RawEvidenceAPI(store)
    except Exception:
        return []
    for method_name in ("search_raw_chunks", "search"):
        method = getattr(api, method_name, None)
        if not callable(method):
            continue
        try:
            return _normalize_results(method(query=query, scope=scope, limit=limit))
        except TypeError:
            try:
                return _normalize_results(method(query, scope=scope, limit=limit))
            except Exception:
                continue
        except Exception:
            continue
    return []


def _store_raw_candidates(store: Any, *, query: str, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    if hasattr(store, "search_raw_chunks") and callable(store.search_raw_chunks):
        try:
            return _normalize_results(store.search_raw_chunks(query=query, scope=scope, limit=limit))
        except Exception:
            pass
    records: list[RecordEnvelope] = []
    try:
        records.extend(store.search(query=query, kinds=["memory"], scope=scope, limit=limit))
    except Exception:
        pass
    try:
        for record in store.list_records(kinds=["memory"], scope=scope, status="active", limit=limit * 2):
            if record.record_id not in {item.record_id for item in records}:
                records.append(record)
    except Exception:
        pass
    query_terms = set(_terms(query))
    results: list[dict[str, Any]] = []
    for record in records:
        if not _is_raw_candidate(record):
            continue
        text = _record_text(record)
        score = _lexical_score(query_terms, text)
        if score <= 0 and not _preference_like(text):
            continue
        results.append({"record": record, "base_score": score})
    return results


def _normalize_results(results: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in list(results or []):
        record = _result_record(item)
        if record is None:
            continue
        normalized.append({"record": record, "base_score": _result_base_score(item)})
    return normalized


def _boosts(*, query: str, text: str, record: Any, task_context: dict, max_time: float | None) -> dict[str, float]:
    boosts: dict[str, float] = {}
    query_terms = set(_terms(query))
    text_terms = set(_terms(text))
    if query_terms and text_terms:
        overlap = len(query_terms & text_terms) / max(1, len(query_terms))
        if overlap:
            boosts["keyword_overlap"] = round(min(0.8, overlap * 0.8), 6)
    quoted = _quoted_phrases(query)
    if quoted and any(phrase.lower() in text.lower() for phrase in quoted):
        boosts["quoted_phrase"] = 0.75
    proper = _proper_nouns(query)
    if proper and any(noun.lower() in text.lower() for noun in proper):
        boosts["proper_noun"] = round(min(0.5, 0.2 * len(proper)), 6)
    if _speaker_matches(record, task_context):
        boosts["speaker_role"] = 0.25
    if _preference_like(text):
        boosts["preference_pattern"] = 0.55
    if _current_fact(record, text):
        boosts["current_fact"] = 0.7
    if _conflict_marker(text):
        boosts["conflict_marker"] = 0.25
    temporal = _temporal_currentness(record, max_time=max_time)
    if temporal:
        boosts["temporal_currentness"] = temporal
    return boosts


def _result_record(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("record") or result.get("chunk") or result.get("item")
    return getattr(result, "record", result)


def _result_base_score(result: Any) -> float:
    if isinstance(result, dict):
        value = result.get("base_score", result.get("score", 0.0))
    else:
        value = getattr(result, "base_score", getattr(result, "score", 0.0))
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _record_payload(record: Any, *, text: str) -> dict[str, Any]:
    if isinstance(record, RecordEnvelope):
        return {
            "record_id": record.record_id,
            "kind": record.kind,
            "title": record.title,
            "source": record.source,
            "text": text,
            "occurred_at": _timestamp_text(record),
        }
    if isinstance(record, dict):
        payload = dict(record)
        payload.setdefault("record_id", str(payload.get("chunk_id") or payload.get("id") or ""))
        payload.setdefault("text", text)
        return payload
    return {
        "record_id": str(getattr(record, "record_id", getattr(record, "chunk_id", ""))),
        "kind": str(getattr(record, "kind", "raw_chunk")),
        "title": str(getattr(record, "title", "")),
        "source": str(getattr(record, "source", "")),
        "text": text,
        "occurred_at": _timestamp_text(record),
    }


def _record_text(record: Any) -> str:
    if isinstance(record, RecordEnvelope):
        content = record.content if isinstance(record.content, dict) else {}
        values = [
            content.get("raw_text"),
            content.get("text"),
            content.get("body"),
            record.summary,
            record.detail,
            record.title,
            *synthetic_preference_texts(str(content.get("raw_text") or content.get("text") or record.summary or "")),
        ]
        return "\n".join(str(value) for value in values if str(value or "").strip())
    if isinstance(record, dict):
        values = [record.get(key) for key in ("raw_text", "text", "body", "summary", "title")]
        values.extend(synthetic_preference_texts(str(record.get("raw_text") or record.get("text") or "")))
        return "\n".join(str(value) for value in values if str(value or "").strip())
    return str(getattr(record, "raw_text", "") or getattr(record, "text", "") or getattr(record, "summary", "") or "")


def _is_raw_candidate(record: Any) -> bool:
    if not isinstance(record, RecordEnvelope):
        return True
    content = record.content if isinstance(record.content, dict) else {}
    memory_type = str(record.meta.get("memory_type") or content.get("memory_type") or "").strip().lower()
    source = str(record.source or "").strip().lower()
    return bool(content.get("raw_text")) or memory_type in {"raw", "raw_chunk", "conversation"} or source in {"raw", "raw_chunk"}


def _lexical_score(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    text_terms = set(_terms(text))
    if not text_terms:
        return 0.0
    return round(len(query_terms & text_terms) / max(1, len(query_terms)), 6)


def _terms(text: str) -> list[str]:
    return [
        term.lower()
        for term in re.findall(r"[\w']+", str(text or ""), flags=re.UNICODE)
        if len(term.strip("'")) > 1
    ]


def _quoted_phrases(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"['\"]([^'\"]{2,})['\"]", str(text or ""))]


def _proper_nouns(text: str) -> list[str]:
    return [match.group(0) for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", str(text or ""))]


def _speaker_matches(record: Any, task_context: dict) -> bool:
    expected = str(task_context.get("speaker") or task_context.get("role") or "user").strip().lower()
    if not expected:
        return False
    if isinstance(record, RecordEnvelope):
        actual = str(record.content.get("speaker") or record.meta.get("speaker") or "").strip().lower()
    elif isinstance(record, dict):
        actual = str(record.get("speaker") or record.get("role") or "").strip().lower()
    else:
        actual = str(getattr(record, "speaker", getattr(record, "role", ""))).strip().lower()
    return bool(actual and actual == expected)


def _preference_like(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "i prefer",
            "i like",
            "i don't like",
            "i dont like",
            "i find",
            "user preference:",
            "prefer ",
            "preference",
        )
    )


def _current_fact(record: Any, text: str) -> bool:
    lowered = str(text or "").lower()
    if "current=true" in lowered or "currently" in lowered or "now prefer" in lowered:
        return True
    for container in _containers(record):
        value = container.get("current")
        if value is True or str(value).strip().lower() == "true":
            return True
        if container.get("valid_from"):
            return True
    return False


def _conflict_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("instead", "changed", "no longer", "but now", "rather than", "conflict"))


def _temporal_currentness(record: Any, *, max_time: float | None) -> float:
    timestamp = _timestamp(record)
    if timestamp is None:
        return 0.0
    if max_time is not None and timestamp >= max_time:
        return 0.6
    # Stable, deterministic recency curve for older evidence.
    days_since_epoch = max(0.0, timestamp / 86400.0)
    return round(min(0.45, math.log1p(days_since_epoch) / 30.0), 6)


def _max_timestamp(results: list[Any]) -> float | None:
    values = [_timestamp(_result_record(item)) for item in results]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _timestamp(record: Any) -> float | None:
    for value in _timestamp_values(record):
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _timestamp_text(record: Any) -> str:
    for value in _timestamp_values(record):
        if str(value or "").strip():
            return str(value)
    return ""


def _timestamp_values(record: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(record, RecordEnvelope):
        values.extend(
            [
                record.content.get("occurred_at"),
                record.content.get("valid_from"),
                record.meta.get("occurred_at"),
                record.meta.get("valid_from"),
                record.time.occurred_at,
                record.time.updated_at,
            ]
        )
    elif isinstance(record, dict):
        values.extend([record.get("occurred_at"), record.get("valid_from"), record.get("updated_at"), record.get("created_at")])
    else:
        values.extend(
            [
                getattr(record, "occurred_at", ""),
                getattr(record, "valid_from", ""),
                getattr(record, "updated_at", ""),
                getattr(record, "created_at", ""),
            ]
        )
    return values


def _parse_timestamp(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _containers(record: Any) -> list[dict[str, Any]]:
    if isinstance(record, RecordEnvelope):
        return [record.content if isinstance(record.content, dict) else {}, record.meta if isinstance(record.meta, dict) else {}]
    if isinstance(record, dict):
        return [record]
    containers: list[dict[str, Any]] = []
    for name in ("content", "meta"):
        value = getattr(record, name, None)
        if isinstance(value, dict):
            containers.append(value)
    return containers
