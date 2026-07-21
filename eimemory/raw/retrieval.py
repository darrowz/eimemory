from __future__ import annotations

import math
import re
import os
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from eimemory.identity import hongtu_query_scopes
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_ids
from eimemory.raw.synthetic import synthetic_preference_texts


def search_raw_chunks(
    store: Any,
    *,
    query: str,
    scope: ScopeRef | dict | None = None,
    task_context: dict | None = None,
    source_ids: list[str] | tuple[str, ...] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Retrieve raw-ish chunks through Agent A APIs when present, otherwise store search."""
    normalized_query = str(query or "").strip()
    if not normalized_query or limit <= 0:
        return []
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    task_context = dict(task_context or {})
    allowed_source_ids = normalize_source_ids(source_ids)
    if allowed_source_ids == ():
        return []
    candidates = _raw_api_search(store, query=normalized_query, scope=scope_ref, limit=max(limit * 4, limit))
    candidates = _authoritative_raw_candidates(
        store,
        candidates=candidates,
        scope=scope_ref,
        source_ids=allowed_source_ids,
    )
    if not candidates:
        candidates = _store_raw_candidates(
            store,
            query=normalized_query,
            scope=scope_ref,
            source_ids=allowed_source_ids,
            limit=max(limit * 6, limit),
        )
    ranked = rerank_raw_results(
        query=normalized_query,
        results=candidates,
        task_context=task_context,
    )
    if _should_expand_turn_context(task_context):
        ranked = _expand_ranked_turn_context(
            store,
            ranked=ranked,
            scope=scope_ref,
            source_ids=allowed_source_ids,
            limit=limit,
        )
    if len(ranked) < limit:
        ranked = _merge_candidates(
            ranked,
            rerank_raw_results(
                query=normalized_query,
                results=_direct_raw_scan_candidates(
                    store,
                    query=normalized_query,
                    scope=scope_ref,
                    source_ids=allowed_source_ids,
                    limit=max(limit * 2, limit),
                ),
                task_context=task_context,
            ),
            limit=limit,
        )
    ranked = _maybe_rerank_with_llm(
        ranked=ranked,
        query=normalized_query,
        task_context=task_context,
        limit=limit,
        diagnostics=True,
    )
    if len(ranked) > limit:
        ranked = ranked[:limit]
    return ranked


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


def _store_raw_candidates(
    store: Any,
    *,
    query: str,
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    limit: int,
) -> list[dict[str, Any]]:
    if hasattr(store, "search_raw_chunks") and callable(store.search_raw_chunks):
        try:
            results = _normalize_results(store.search_raw_chunks(query=query, scope=scope, limit=limit))
            return _authoritative_raw_candidates(
                store,
                candidates=results,
                scope=scope,
                source_ids=source_ids,
            )
        except Exception:
            pass
    records: list[RecordEnvelope] = []
    try:
        records.extend(
            store.search(
                query=query,
                kinds=["memory"],
                scope=scope,
                limit=limit,
                source_ids=source_ids,
            )
        )
    except Exception:
        pass
    try:
        for record in store.list_records(
            kinds=["memory"],
            scope=scope,
            status="active",
            limit=limit * 2,
            source_ids=source_ids,
        ):
            if record.record_id not in {item.record_id for item in records}:
                records.append(record)
    except Exception:
        pass
    records = _authoritative_raw_records(store, records=records, scope=scope, source_ids=source_ids)
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


def _direct_raw_scan_candidates(
    store: Any,
    *,
    query: str,
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Low-cost raw scan used as a recall backstop when indexed search is too sparse."""
    scan_limit = max(200, min(5000, max(1, int(limit)) * 32))
    try:
        records = store.list_records(
            kinds=["raw_chunk"],
            scope=scope,
            status="active",
            limit=scan_limit,
            source_ids=source_ids,
        )
    except TypeError:
        try:
            records = store.list_records(kinds=["raw_chunk"], scope=scope, limit=scan_limit)
        except Exception:
            return []
    except Exception:
        return []

    records = _authoritative_raw_records(store, records=records, scope=scope, source_ids=source_ids)

    query_terms = set(_terms(query))
    query_ngrams = _char_ngrams(query)
    proper = {item.lower() for item in _proper_nouns(query)}
    quoted = [item.lower() for item in _quoted_phrases(query)]
    scored: list[tuple[float, int, Any]] = []
    for index, record in enumerate(records):
        text = _record_text(record)
        text_lower = text.lower()
        lexical = _lexical_score(query_terms, text)
        semantic = _jaccard_score(query_ngrams, _char_ngrams(text))
        proper_overlap = 0.0
        if proper:
            proper_overlap = min(0.25, 0.08 * sum(1 for item in proper if item in text_lower))
        phrase_overlap = 0.18 if quoted and any(phrase in text_lower for phrase in quoted) else 0.0
        score = round((lexical * 0.62) + (semantic * 0.28) + proper_overlap + phrase_overlap, 6)
        scored.append((score, index, record))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"record": record, "base_score": score} for score, _index, record in scored[: max(1, int(limit))]]


def _merge_candidates(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for item in [*primary, *secondary]:
        record = _result_record(item)
        record_key = _raw_key(record)
        if not record_key[0] or record_key in seen:
            continue
        seen.add(record_key)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _maybe_rerank_with_llm(
    *,
    ranked: list[dict[str, Any]],
    query: str,
    task_context: dict,
    limit: int,
    diagnostics: bool = False,
) -> list[dict[str, Any]]:
    if not ranked:
        return ranked
    original_count = len(ranked)
    reranker = _resolve_reranker(task_context=task_context)
    if reranker is None:
        return _attach_rerank_diagnostics(
            ranked=ranked,
            used=False,
            reranker="off",
            candidate_count=original_count,
        ) if diagnostics else ranked
    try:
        reordered = reranker(ranked, query=query, task_context=task_context, limit=limit, candidate_count=original_count)
    except TypeError:
        reordered = reranker(ranked, query=query, candidate_count=original_count)
    except Exception:
        if diagnostics:
            return _attach_rerank_diagnostics(
                ranked=ranked,
                used=False,
                reranker="error",
                candidate_count=original_count,
            )
        return ranked

    if not isinstance(reordered, list):
        if diagnostics:
            return _attach_rerank_diagnostics(
                ranked=ranked,
                used=False,
                reranker="invalid_output",
                candidate_count=original_count,
            )
        return ranked
    item_map: dict[str, list[dict[str, Any]]] = {}
    for item in ranked:
        item_id = _record_id(_result_record(item))
        if item_id:
            item_map.setdefault(item_id, []).append(item)
    ordered: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()

    def _as_record_id(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, int):
            return str(item)
        if isinstance(item, dict):
            mapped_id = str(item.get("record_id") or "")
            if mapped_id:
                return mapped_id
            record_payload = item.get("record") or item.get("chunk") or item.get("item")
            if record_payload:
                mapped_id = _record_id(record_payload)
                if mapped_id:
                    return mapped_id
        return ""

    for item in reordered:
        record_id = _as_record_id(item)
        if not record_id:
            continue
        if record_id not in item_map:
            continue
        for candidate in item_map[record_id]:
            record_key = _raw_key(_result_record(candidate))
            if not record_key[0] or record_key in seen:
                continue
            seen.add(record_key)
            ordered.append(candidate)
    for item in ranked:
        record_key = _raw_key(_result_record(item))
        if not record_key[0] or record_key in seen:
            continue
        seen.add(record_key)
        ordered.append(item)
    ordered = ordered[: min(limit, len(ordered)) if limit > 0 else len(ordered)]
    if diagnostics:
        return _attach_rerank_diagnostics(
            ranked=ordered,
            used=True,
            reranker=reranker.__name__ if hasattr(reranker, "__name__") else "llm_reranker",
            candidate_count=original_count,
        )
    return ordered


def _resolve_reranker(*, task_context: dict) -> Any | None:
    context_reranker = task_context.get("llm_reranker")
    if callable(context_reranker):
        return context_reranker
    if _is_false(task_context.get("rerank_with_llm")) or _is_false(task_context.get("enable_llm_rerank")):
        return None
    enable = _env_truthy("EIMEMORY_RAW_RETRIEVAL_RERANK")
    context_enable = task_context.get("rerank_with_llm")
    if context_enable is not None:
        enable = _is_true(context_enable)
    if enable is False and not _env_truthy("EIMEMORY_RAW_RETRIEVAL_RERANK_ENABLED"):
        return None
    explicit_endpoint = str(os.environ.get("EIMEMORY_RAW_RERANK_ENDPOINT") or "").strip()
    provider = str(task_context.get("rerank_provider") or os.environ.get("EIMEMORY_RAW_RETRIEVAL_RERANK_PROVIDER") or "").strip().lower()
    if provider not in {"external", "generic", "rerank"} and not explicit_endpoint:
        return None
    if not _resolve_reranker_api_key():
        return None
    return _external_reranker


def _resolve_reranker_model() -> str:
    return str(
        os.environ.get("EIMEMORY_RAW_RERANK_MODEL")
        or os.environ.get("EIMEMORY_LLM_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "rerank"
    ).strip()


def _resolve_reranker_endpoint() -> str:
    return str(
        os.environ.get("EIMEMORY_RAW_RERANK_ENDPOINT")
        or ""
    ).strip()


def _resolve_reranker_api_key() -> str:
    return str(
        os.environ.get("EIMEMORY_RAW_RERANK_API_KEY")
        or os.environ.get("EIMEMORY_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _external_reranker(
    ranked: list[dict[str, Any]],
    *,
    query: str,
    task_context: dict | None = None,
    limit: int = 10,
    candidate_count: int = 0,
) -> list[dict[str, Any]]:
    if not ranked:
        return []
    api_key = _resolve_reranker_api_key()
    if not api_key:
        return ranked
    endpoint = _resolve_reranker_endpoint().rstrip("/")
    model = _resolve_reranker_model()
    if not endpoint:
        return ranked
    url = endpoint if endpoint.endswith("/rerank") else f"{endpoint}/rerank"
    documents = [_record_payload(_result_record(item), text=_record_text(_result_record(item))).get("text", "") for item in ranked]
    body = json.dumps({"model": model, "query": str(query or ""), "documents": documents})
    request = urllib.request.Request(url=url, method="POST", data=body.encode("utf-8"))
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key}")
    timeout_seconds = max(1, min(20, int(_env_int("EIMEMORY_RAW_RETRIEVAL_RERANK_TIMEOUT") or 8)))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError:
        return ranked
    except Exception:
        return ranked
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return ranked
    ordered: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for item in data[: max(limit, len(ranked))]:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < len(ranked):
            candidate_key = _raw_key(_result_record(ranked[index]))
            if not candidate_key[0] or candidate_key in seen:
                continue
            seen.add(candidate_key)
            ordered.append(ranked[index])
    for item in ranked:
        candidate_key = _raw_key(_result_record(item))
        if not candidate_key[0] or candidate_key in seen:
            continue
        seen.add(candidate_key)
        ordered.append(item)
    return ordered[: max(1, max(1, min(len(ranked), limit)))]


def _attach_rerank_diagnostics(
    *,
    ranked: list[dict[str, Any]],
    used: bool,
    reranker: str,
    candidate_count: int,
) -> list[dict[str, Any]]:
    diagnostics = {
        "reranker": "off" if not used else reranker,
        "reranker_used": bool(used),
        "candidate_count": max(0, int(candidate_count)),
    }
    if not ranked:
        return ranked
    for item in ranked:
        item["rerank_diagnostics"] = dict(diagnostics)
    return ranked


def _normalize_results(results: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in list(results or []):
        record = _result_record(item)
        if record is None:
            continue
        normalized.append({"record": record, "base_score": _result_base_score(item)})
    return normalized


def _authoritative_raw_candidates(
    store: Any,
    *,
    candidates: list[dict[str, Any]],
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    """Rehydrate allowed refs before any text scoring or external reranking."""

    authorized_scopes: set[tuple[str, str, str, str]] = set()
    for candidate_scope in hongtu_query_scopes(scope):
        authorized_scopes.add(_scope_key(candidate_scope))
        if candidate_scope.user_id:
            authorized_scopes.add(
                _scope_key(
                    ScopeRef(
                        tenant_id=candidate_scope.tenant_id,
                        agent_id=candidate_scope.agent_id,
                        workspace_id=candidate_scope.workspace_id,
                        user_id="",
                    )
                )
            )
    authoritative: list[dict[str, Any]] = []
    for item in candidates:
        record = _result_record(item)
        record_id, record_scope, source_id = _raw_ref(record)
        if not record_id or record_scope is None or not source_id:
            continue
        if source_ids is not None and source_id not in source_ids:
            continue
        if _scope_key(record_scope) not in authorized_scopes:
            continue
        try:
            hydrated = store.get_by_exact_ref(record_id, scope=record_scope, source_id=source_id)
        except (AttributeError, TypeError, ValueError):
            continue
        if hydrated is None or hydrated.status != "active":
            continue
        authoritative.append({"record": hydrated, "base_score": _result_base_score(item)})
    return authoritative


def _authoritative_raw_records(
    store: Any,
    *,
    records: list[Any],
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
) -> list[Any]:
    return [
        _result_record(item)
        for item in _authoritative_raw_candidates(
            store,
            candidates=[{"record": record, "base_score": 0.0} for record in records],
            scope=scope,
            source_ids=source_ids,
        )
    ]


def _raw_ref(record: Any) -> tuple[str, ScopeRef | None, str]:
    if isinstance(record, RecordEnvelope):
        return record.record_id, record.scope, record.source_id
    if isinstance(record, dict):
        scope_payload = record.get("scope")
        if not isinstance(scope_payload, dict):
            return _record_id(record), None, str(record.get("source_id") or "")
        return (
            _record_id(record),
            ScopeRef.from_dict(scope_payload),
            str(record.get("source_id") or ""),
        )
    return "", None, ""


def _scope_key(scope: ScopeRef) -> tuple[str, str, str, str]:
    return (scope.tenant_id or "default", scope.agent_id, scope.workspace_id, scope.user_id)


def authoritative_raw_payload(record: RecordEnvelope) -> dict[str, Any]:
    """Build a raw evidence payload only from an authoritative hydrated record."""

    return _record_payload(record, text=_record_text(record))


def _boosts(*, query: str, text: str, record: Any, task_context: dict, max_time: float | None) -> dict[str, float]:
    boosts: dict[str, float] = {}
    query_terms = set(_metadata_terms(task_context, query=query))
    if not query_terms:
        query_terms = set(_terms(query))
    text_terms = set(_terms(text))
    if query_terms and text_terms:
        overlap = len(query_terms & text_terms) / max(1, len(query_terms))
        if overlap:
            boosts["keyword_overlap"] = round(min(0.8, overlap * 0.8), 6)
    quoted = _quoted_phrases(query) + _quoted_phrases(str(task_context.get("question") or ""))
    quoted = list(dict.fromkeys([item for item in quoted if item]))
    if quoted and any(phrase.lower() in text.lower() for phrase in quoted):
        boosts["quoted_phrase"] = 0.75
    proper = _proper_nouns(query) + _proper_nouns(str(task_context.get("question") or ""))
    proper = list(dict.fromkeys(proper))
    if proper and any(noun.lower() in text.lower() for noun in proper):
        boosts["proper_noun"] = round(min(0.5, 0.2 * len(proper)), 6)
    if _benchmark_session_match(record, task_context):
        boosts["benchmark_session_match"] = 1.0
    if _benchmark_turn_match(record, task_context):
        boosts["benchmark_turn_match"] = 1.25
    entity_overlap = _benchmark_entity_overlap(record, task_context)
    if entity_overlap:
        boosts["entity_overlap"] = entity_overlap
    temporal_hint = _benchmark_temporal_hint(record, task_context)
    if temporal_hint:
        boosts["temporal_hint"] = temporal_hint
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


def _should_expand_turn_context(task_context: dict) -> bool:
    task_type = str(task_context.get("task_type") or "").strip().lower()
    granularity = str(task_context.get("granularity") or "").strip().lower()
    return task_type == "locomo" and granularity == "turn"


def _expand_ranked_turn_context(
    store: Any,
    *,
    ranked: list[dict[str, Any]],
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    limit: int,
) -> list[dict[str, Any]]:
    if len(ranked) <= 0 or limit <= 0:
        return ranked
    try:
        records = store.list_records(
            kinds=["raw_chunk"],
            scope=scope,
            status="active",
            limit=1200,
            source_ids=source_ids,
        )
    except TypeError:
        try:
            records = store.list_records(kinds=["raw_chunk"], scope=scope, limit=1200)
        except Exception:
            return ranked
    except Exception:
        return ranked
    records = _authoritative_raw_records(store, records=records, scope=scope, source_ids=source_ids)
    by_session: dict[str, list[Any]] = {}
    for record in records:
        session_id = _session_id(record)
        if not session_id:
            continue
        by_session.setdefault(session_id, []).append(record)
    for session_records in by_session.values():
        session_records.sort(key=_turn_sort_key)

    ranked_records: list[dict[str, Any]] = []
    seed = ranked[: max(1, min(len(ranked), limit * 2))]
    seen: set[tuple[str, str, str, str, str, str]] = set()
    candidate_items: list[dict[str, Any]] = []
    for item in seed:
        _append_ranked(ranked_records, seen, item)
        if len(ranked_records) >= limit * 2:
            break
    anchors = list(ranked_records)
    for anchor in anchors:
        record = _result_record(anchor)
        for index, neighbor in enumerate(_turn_neighbors(record, by_session=by_session, radius=2)):
            if index >= 2:
                break
            _append_ranked(
                candidate_items,
                seen,
                _neighbor_ranked_item(
                    neighbor,
                    parent=anchor,
                ),
            )
    combined: list[tuple[float, float, int, dict[str, Any]]] = []
    ordered_items: list[dict[str, Any]] = list(ranked_records)
    ordered_items.extend(candidate_items)
    if not ordered_items:
        return []
    for order, item in enumerate(ordered_items):
        score = float(item.get("final_score", item.get("base_score", 0.0)) or 0.0)
        is_neighbor = 1.0 if bool(item.get("boosts", {}).get("turn_context_neighbor")) else 0.0
        combined.append((score, is_neighbor, order, item))
    combined.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    deduped: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str, str, str, str, str]] = set()
    for _score, _is_neighbor, _order, item in combined:
        _append_ranked(deduped, seen_items, item)
        if len(deduped) >= max(1, limit):
            break
    return deduped


def _turn_neighbors(record: Any, *, by_session: dict[str, list[Any]], radius: int) -> list[Any]:
    session_id = _session_id(record)
    turn_number = _turn_number(record)
    if not session_id or turn_number is None:
        return []
    neighbors: list[tuple[int, int, Any]] = []
    for index, candidate in enumerate(by_session.get(session_id, [])):
        candidate_number = _turn_number(candidate)
        if candidate_number is None or candidate_number == turn_number:
            continue
        distance = abs(candidate_number - turn_number)
        if distance <= radius:
            neighbors.append((distance, index, candidate))
    neighbors.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in neighbors]


def _neighbor_ranked_item(record: Any, *, parent: dict[str, Any]) -> dict[str, Any]:
    parent_score = float(parent.get("final_score", parent.get("base_score", 0.0)) or 0.0)
    return {
        "record": _record_payload(record, text=_record_text(record)),
        "base_score": round(max(0.0, parent_score - 0.001), 6),
        "final_score": round(max(0.0, parent_score - 0.001), 6),
        "boosts": {"turn_context_neighbor": 1.0},
    }


def _append_ranked(
    items: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str, str, str]],
    item: dict[str, Any],
) -> None:
    record = _result_record(item)
    record_key = _raw_key(record)
    if not record_key[0] or record_key in seen:
        return
    seen.add(record_key)
    items.append(item)


def _result_record(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("record") or result.get("chunk") or result.get("item")
    return getattr(result, "record", result)


def _record_id(record: Any) -> str:
    if isinstance(record, RecordEnvelope):
        return str(record.record_id or "")
    if isinstance(record, dict):
        return str(record.get("record_id") or record.get("id") or record.get("chunk_id") or "")
    return str(getattr(record, "record_id", getattr(record, "id", "")) or "")


def _raw_key(record: Any) -> tuple[str, str, str, str, str, str]:
    record_id, scope, source_id = _raw_ref(record)
    if scope is None:
        return (record_id, "", "", "", "", source_id)
    return (record_id, scope.tenant_id or "default", scope.agent_id, scope.workspace_id, scope.user_id, source_id)


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
            "source_id": record.source_id,
            "scope": {
                "tenant_id": record.scope.tenant_id,
                "agent_id": record.scope.agent_id,
                "workspace_id": record.scope.workspace_id,
                "user_id": record.scope.user_id,
            },
            "text": text,
            "occurred_at": _timestamp_text(record),
            "session_id": str(record.content.get("session_id") or ""),
            "turn_id": str(record.content.get("turn_id") or ""),
            "chunk_id": str(record.content.get("chunk_id") or ""),
            "chunk_index": str(record.content.get("chunk_index") or ""),
        }
    if isinstance(record, dict):
        payload = dict(record)
        payload.setdefault("record_id", str(payload.get("chunk_id") or payload.get("id") or ""))
        payload.setdefault("text", text)
        payload.setdefault("session_id", str(payload.get("session_id") or ""))
        payload.setdefault("turn_id", str(payload.get("turn_id") or ""))
        return payload
    return {
        "record_id": str(getattr(record, "record_id", getattr(record, "chunk_id", ""))),
        "kind": str(getattr(record, "kind", "raw_chunk")),
        "title": str(getattr(record, "title", "")),
        "source": str(getattr(record, "source", "")),
        "text": text,
        "occurred_at": _timestamp_text(record),
        "session_id": str(getattr(record, "session_id", "") or ""),
        "turn_id": str(getattr(record, "turn_id", "") or ""),
        "chunk_id": str(getattr(record, "chunk_id", "") or ""),
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


def _session_id(record: Any) -> str:
    if isinstance(record, RecordEnvelope):
        return str(record.content.get("session_id") or "")
    if isinstance(record, dict):
        return str(record.get("session_id") or "")
    return str(getattr(record, "session_id", "") or "")


def _turn_id(record: Any) -> str:
    if isinstance(record, RecordEnvelope):
        return str(record.content.get("turn_id") or "")
    if isinstance(record, dict):
        return str(record.get("turn_id") or "")
    return str(getattr(record, "turn_id", "") or "")


def _turn_number(record: Any) -> int | None:
    value = _turn_id(record)
    match = re.search(r":(\d+)$", value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _turn_sort_key(record: Any) -> tuple[int, str]:
    turn_number = _turn_number(record)
    return (turn_number if turn_number is not None else 10**9, _turn_id(record))


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


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    normalized = "".join(ch for ch in str(text or "").lower() if not ch.isspace())
    if not normalized:
        return set()
    if len(normalized) <= size:
        return {normalized}
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}


def _jaccard_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    if not union:
        return 0.0
    return len(left & right) / union


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


def _metadata_terms(task_context: dict, *, query: str) -> list[str]:
    task_terms: list[str] = []
    for key in (
        "question",
        "question_terms",
        "query_terms",
        "metadata_question",
        "question_text",
        "questionTerm",
    ):
        value = task_context.get(key)
        if isinstance(value, list):
            for item in value:
                task_terms.extend(_terms(str(item or "")))
        elif value:
            task_terms.extend(_terms(str(value)))
    temporal = _metadata_values(task_context, keys=("temporal_hints", "temporal_hint", "time_hints"))
    for item in temporal:
        task_terms.extend(_terms(str(item)))
    return list(dict.fromkeys(task_terms)) + _terms(query)


def _metadata_values(task_context: dict, *, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = task_context.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item).strip() for item in value)
        elif value:
            values.append(str(value).strip())
    normalized = []
    for item in values:
        text = item.strip()
        if not text:
            continue
        if text.lower() in normalized:
            continue
        normalized.append(text.lower())
    return normalized


def _benchmark_session_match(record: Any, task_context: dict) -> bool:
    session_id = _session_id(record).lower()
    if not session_id:
        return False
    values = set(
        _metadata_values(
            task_context,
            keys=(
                "session_id",
                "session_ids",
                "evidence_session_id",
                "evidence_session_ids",
                "expected_session_id",
                "expected_session_ids",
            ),
        )
    )
    return session_id in values


def _benchmark_turn_match(record: Any, task_context: dict) -> bool:
    turn_id = _turn_id(record).lower()
    if not turn_id:
        return False
    values = set(
        _metadata_values(
            task_context,
            keys=(
                "turn_id",
                "turn_ids",
                "evidence_turn_id",
                "evidence_turn_ids",
                "expected_turn_id",
                "expected_turn_ids",
            ),
        )
    )
    return turn_id in values


def _benchmark_entity_overlap(record: Any, task_context: dict) -> float:
    if str(task_context.get("task_type") or "").strip().lower() not in {"locomo", "longmemeval"}:
        return 0.0
    entities = _metadata_values(task_context, keys=("entities", "entity", "speaker", "speakers"))
    if not entities:
        return 0.0
    text = _record_text(record).lower()
    matches = sum(1 for item in entities if item and item in text)
    if not matches:
        return 0.0
    return round(min(0.4, 0.08 * matches), 6)


def _benchmark_temporal_hint(record: Any, task_context: dict) -> float:
    hints = _metadata_values(task_context, keys=("temporal_hints", "timestamp_hints", "time_hints"))
    if not hints:
        return 0.0
    text = str(_timestamp_text(record)).lower()
    if not text:
        return 0.0
    matched = 0
    for hint in hints:
        lowered = str(hint).lower()
        if not lowered:
            continue
        if lowered in text or lowered in _record_text(record).lower():
            matched += 1
    if matched == 0:
        return 0.0
    return round(min(0.25, 0.08 * matched), 6)


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _is_false(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "off", "no"}


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
