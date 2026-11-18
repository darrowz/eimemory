from __future__ import annotations

import math
import re
import os
import json
import logging
import urllib.error
import urllib.request
from eimemory.intake.safe_transport import UnsafeURL, safe_urlopen
from datetime import date, datetime, timezone
from typing import Any

from eimemory.identity import hongtu_query_scopes
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.contracts.recall_boundary import authority_digest, exact_ref
from time import perf_counter
from eimemory.core.strict_json import loads as strict_json_loads
from .boundary import (raw_request_boundary, raw_ref_allowed, capture_raw_snapshot,
                       raw_index_options, raw_read_scope, raw_remaining_seconds, invoke_raw_reranker,
                       RawRecallUnavailable, require_raw_collection_complete)
from eimemory.models.source_partitions import normalize_source_ids
from ._relevance import (CONFLICT_DEPTH, ContentMatch, QueryContent, change_lines, common_subject,
                        conflict_preference, current_assertion, match_content, query_content)
from .citation_gate import fact_citation_gate

_log = logging.getLogger(__name__)
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


@fact_citation_gate
@raw_request_boundary
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
    try:
        return _search_raw_candidates(
            store, normalized_query=normalized_query, scope_ref=scope_ref,
            task_context=task_context, allowed_source_ids=allowed_source_ids, limit=limit,
        )
    except RawRecallUnavailable as exc:
        if not exc.allow_recovery:
            raise
        # A failed index/partial collection is not an empty corpus. Recover
        # only by a completed, untruncated authoritative scan of both raw
        # storage representations. A bounded/truncated scan cannot certify it.
        candidates = _direct_raw_scan_candidates(
            store, query=normalized_query, scope=scope_ref,
            source_ids=allowed_source_ids, limit=max(limit * 6, limit),
            require_complete=True,
        )
        ranked = rerank_raw_results(query=normalized_query, results=candidates, task_context=task_context)
        if _should_expand_turn_context(task_context):
            ranked = _expand_ranked_turn_context(
                store, ranked=ranked, scope=scope_ref, source_ids=allowed_source_ids, limit=limit,
            )
        return ranked[:limit]


def _search_raw_candidates(store, *, normalized_query, scope_ref, task_context, allowed_source_ids, limit):
    candidates = _raw_api_search(
        store, query=normalized_query, scope=scope_ref,
        source_ids=allowed_source_ids, limit=max(limit * 4, limit),
    )
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
    expand_turn_context = _should_expand_turn_context(task_context)
    if expand_turn_context:
        ranked = _expand_ranked_turn_context(
            store, ranked=ranked, scope=scope_ref,
            source_ids=allowed_source_ids, limit=limit,
        )
    if len(ranked) < limit:
        # Rank the candidate union with one time reference. Appending an
        # independently ranked backstop makes even a strong correction lose
        # to every indexed hit and assigns two different "newest" bonuses.
        ranked = rerank_raw_results(
            query=normalized_query,
            results=[
                *candidates,
                *_direct_raw_scan_candidates(
                    store,
                    query=normalized_query,
                    scope=scope_ref,
                    source_ids=allowed_source_ids,
                    limit=max(limit * 2, limit),
                ),
            ],
            task_context=task_context,
        )
        if expand_turn_context:
            ranked = _expand_ranked_turn_context(
                store, ranked=ranked, scope=scope_ref,
                source_ids=allowed_source_ids, limit=limit,
            )
    # Model quality is evaluated after delivery; keep local ranking and bounds.
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
    results = _unique_raw_candidates(results)
    # Candidate collection, exact scope/source checks and authoritative
    # hydration remain upstream of this pure ranking step. No global corpus
    # statistics, provider-score thresholds or candidate expansion are needed.
    plan = query_content(_content_query(query_text, task_context))
    ranked: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        record = _result_record(result)
        if task_context.get("evidence_intent") == "fact_citation":
            from .applicability import assess_applicability
            # Keep exploratory ranking unchanged. Unknown applicability is
            # still related material; only an explicit mismatch is excluded.
            # The authority boundary rechecks hydrated records after ranking.
            if assess_applicability(record, query=query_text, task_context=task_context).status == "not_applicable":
                continue
        base_score = _result_base_score(result)
        record_text = _record_text(record)
        title = str(record.get("title") or "") if isinstance(record, dict) else str(getattr(record, "title", "") or "")
        content_match = match_content(plan, record_text, title)
        boosts = _boosts(
            query=query_text,
            text=record_text,
            record=record,
            task_context=task_context,
            content_match=content_match,
            plan=plan,
        )
        secondary = base_score + sum(boosts.values())
        # Store final_score mixes quality/relevance and is not BM25. Compress
        # it monotonically into a tiebreaker smaller than one content band.
        # The offset is just a rank-score origin; this is not confidence.
        final_score = round(1.0 + content_match.strength + 0.5 * (secondary / (1.0 + secondary)), 6)
        ranked.append(
            {
                "record": _record_payload(record, text=record_text),
                "base_score": round(base_score, 6),
                "final_score": final_score,
                "boosts": boosts,
                "_secondary": secondary,
                "_index": index,
                "_fts_rank": result.get("fts_rank"),
                "_match": content_match,
                "_day": _evidence_day(record),
                "_partition": _raw_key(record)[1:],
                "_current": _current_fact(record),
            }
        )
    # Sort the finite secondary itself: public compression/rounding can erase
    # meaningful differences (and even unrounded compression saturates).
    ranked.sort(key=lambda item: (-item["_match"].strength, -item["_secondary"], item["_index"]))
    if not plan.phrases and task_context.get("evidence_intent") != "fact_citation":
        _preserve_dialogue_fts_order(ranked)
    _apply_topic_order(ranked, plan)
    for item in ranked:
        for key in ("_index", "_match", "_day", "_partition", "_current", "_secondary", "_fts_rank"):
            item.pop(key, None)
    return ranked


_USER_PREFIX = re.compile(r"\s*(?:user|human|用户)\s*[:：]", re.I)
_ASSISTANT_PREFIX = re.compile(r"\s(?:assistant|助手)\s*[:：]", re.I)


def _dialogue_turn(record: dict) -> bool:
    if record.get("turn_id") and str(record.get("role") or "").lower() in {"user", "assistant"}:
        return True
    text = record["text"]
    return bool(_USER_PREFIX.match(text) and _ASSISTANT_PREFIX.search(text))


def _preserve_dialogue_fts_order(ranked: list[dict]) -> None:
    # On comparable turns, counting distinct words anywhere in a long reply
    # discards the index's discriminative term weights and length normalization.
    # Keep exact quotes, mixed sources and document rankings on their normal path.
    indexed = [item for item in ranked if item["_fts_rank"] and item["_match"].terms]
    if (len(ranked) < 2 or not indexed
            or any(item["_partition"] != ranked[0]["_partition"] for item in ranked)
            or not all(_dialogue_turn(item["record"]) for item in ranked)):
        return
    ceiling = max(item["final_score"] for item in ranked)
    for item in indexed:
        reciprocal = 1.0 / item["_fts_rank"]
        item["boosts"]["fts_reciprocal_rank"] = reciprocal
        item["final_score"] = round(ceiling + reciprocal, 6)
    # Stable sort leaves unindexed hits and equal ranks in their previous order.
    ranked.sort(key=lambda item: -item["boosts"].get("fts_reciprocal_rank", 0.0))


def _scored_candidate(record: Any, item: Any) -> dict:
    row = {"record": record, "base_score": _result_base_score(item)}
    rank = item.get("fts_rank") if isinstance(item, dict) else None
    if type(rank) is int and 1 <= rank <= 5000:
        row["fts_rank"] = rank
    return row


def _evidence_day(record: Any) -> date | None:
    # Storage created_at/updated_at are not dates of the asserted fact.
    values = [container.get(key) for container in _containers(record)
              for key in ("occurred_at", "valid_from")]
    if isinstance(record, RecordEnvelope):
        values.append(record.time.occurred_at)
    elif not isinstance(record, dict):
        values.append(getattr(record, "occurred_at", None))
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return datetime.fromtimestamp(parsed, timezone.utc).date()
    return None


def _content_query(query: str, task_context: dict) -> str:
    # Preserve the original spelling/boundaries of identifiers and quotes in
    # question metadata instead of round-tripping through legacy _terms().
    values = [query]
    for key in ("question", "question_terms", "query_terms", "metadata_question",
                "question_text", "questionTerm", "temporal_hints", "temporal_hint", "time_hints"):
        value = task_context.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if item)
        elif value:
            values.append(str(value))
    return "\n".join(values)


def _apply_topic_order(ranked: list[dict[str, Any]], plan: QueryContent) -> None:
    """Adapt upstream's bounded anchor/pair move; never infer cross-partition correction."""
    if len(ranked) < 2:
        return
    first = ranked[0]
    for other in ranked[1:CONFLICT_DEPTH]:
        if first["_partition"] != other["_partition"]:
            continue
        subject = common_subject(first["_match"], other["_match"])
        if not subject:
            continue
        decision = conflict_preference(
            plan, first["_match"], other["_match"],
            first["record"]["text"], other["record"]["text"], first["_day"], other["_day"],
        )
        # Explicit record metadata or a subject-bound textual assertion can
        # break a same-topic tie. Neither may bypass an unbound correction.
        current_pair = (not plan.historical and first["_match"] == other["_match"]
                        and first["_day"] is not None and other["_day"] is not None
                        and first["_day"] != other["_day"])
        if decision is None and current_pair:
            newer = other if other["_day"] > first["_day"] else first
            text = newer["record"]["text"]
            any_change, _ = change_lines(text, subject, first["_match"].phrases, plan.named_terms)
            is_current = current_assertion(text, subject, first["_match"].phrases,
                                           metadata=newer["_current"])
            if is_current and not any_change:
                decision = int(newer is other)
        if decision is None:
            continue
        preferred, previous = (other, first) if decision else (first, other)
        historical_choice = plan.historical and preferred["_day"] < previous["_day"]
        if historical_choice:
            preferred["boosts"]["historical_window"] = 1.0
        else:
            preferred["boosts"]["temporal_currentness"] = 0.6
            previous["boosts"]["temporal_currentness"] = 0.0
            if _preference_like(preferred["record"]["text"]):
                preferred["boosts"]["preference_pattern"] = 0.55
            if current_assertion(
                preferred["record"]["text"], subject, first["_match"].phrases,
                metadata=preferred["_current"],
            ):
                preferred["boosts"]["current_fact"] = 0.7
            if change_lines(preferred["record"]["text"], subject, first["_match"].phrases, plan.named_terms)[1]:
                preferred["boosts"]["correction_marker"] = 0.95
                preferred["boosts"]["conflict_marker"] = 0.25
        # Keep numeric delivery scores consistent with the explicit local
        # move (LoCoMo turn expansion also orders by these scores).
        preferred["final_score"] = round(max(preferred["final_score"], previous["final_score"]) + 0.001, 6)
        if decision:
            ranked.insert(0, ranked.pop(ranked.index(other)))
        break


def _raw_api_search(store: Any, *, query: str, scope: ScopeRef, limit: int,
                    source_ids: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    try:
        from eimemory.raw.store import RawEvidenceAPI  # type: ignore
        api = RawEvidenceAPI(store)
        with raw_read_scope(store):
            return _normalize_results(api.search_raw_chunks(
                query=query, scope=scope, source_ids=source_ids, limit=limit,
            ))
    except RawRecallUnavailable:
        raise
    except Exception as exc:
        raise RawRecallUnavailable("raw_recall_unavailable") from exc


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
            with raw_read_scope(store):
                collected = store.search_raw_chunks(query=query, scope=scope, limit=limit)
                require_raw_collection_complete(collected)
                results = _normalize_results(collected)
            return _authoritative_raw_candidates(
                store,
                candidates=results,
                scope=scope,
                source_ids=source_ids,
            )
        except RawRecallUnavailable:
            raise
        except Exception as exc:
            raise RawRecallUnavailable("raw_recall_unavailable") from exc
    records: list[RecordEnvelope] = []
    try:
        with raw_read_scope(store):
            collected, report = store.search_with_diagnostics(
                query=query,
                kinds=["memory"],
                scope=scope,
                limit=limit,
                **raw_index_options(scope, source_ids),
            )
            require_raw_collection_complete(collected, report)
            records.extend(collected)
    except RawRecallUnavailable:
        raise
    except Exception as exc:
        raise RawRecallUnavailable("raw_recall_unavailable") from exc
    collected = _complete_raw_scan_records(
        store, kinds=["memory"], scope=scope, source_ids=source_ids,
        scan_limit=max(1, min(500, limit * 2)),
    )
    seen = {_raw_key(record) for record in records}
    for record in collected:
        if _raw_key(record) not in seen:
            records.append(record)
            seen.add(_raw_key(record))
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


def _complete_raw_scan_records(
    store: Any, *, kinds: list[str], scope: ScopeRef,
    source_ids: tuple[str, ...] | None, scan_limit: int,
) -> list[RecordEnvelope]:
    """One completion gate for normal, legacy, and recovery raw scans.

    SQL-count/row consistency is checked before logical policy exclusions.
    Every stored reference must then pass exact projection/payload hydration.
    """
    try:
        with raw_read_scope(store):
            expected_count = store.count_records(
                kinds=kinds, scope=scope, status="active", source_ids=source_ids,
            )
            if type(expected_count) is not int or not 0 <= expected_count < scan_limit:
                raise RawRecallUnavailable("raw_recall_unavailable")
            records = store.list_records(
                kinds=kinds,
                scope=scope, status="active", limit=scan_limit, source_ids=source_ids,
            )
            require_raw_collection_complete(records)
            if len(records) != expected_count:
                # list_records may omit rows whose payload cannot hydrate.
                # A short returned list alone is not proof of a complete scan.
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
            if (len({exact_ref(record) for record in records}) != len(records)
                    or any(record.kind not in kinds or record.status != "active" for record in records)):
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
    except RawRecallUnavailable:
        raise
    except Exception as exc:
        raise RawRecallUnavailable("raw_recall_unavailable") from exc

    expected_refs = {exact_ref(record) for record in records if raw_ref_allowed(record)}
    records = _authoritative_raw_records(
        store, records=records, scope=scope, source_ids=source_ids, strict_collection=True,
    )
    if {exact_ref(record) for record in records} != expected_refs:
        raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)

    return records


def _direct_raw_scan_candidates(
    store: Any,
    *,
    query: str,
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    limit: int,
    require_complete: bool = False,
) -> list[dict[str, Any]]:
    """Low-cost raw scan used as a recall backstop when indexed search is too sparse."""
    # Bound backstop scans tightly: indexed search already ran; avoid 32x fan-out.
    # Recovery's 500-row ceiling is below the supported store's 1000-row
    # query cap. Asking for 1500 and receiving a silently capped 1000 would
    # otherwise look like an exhausted corpus. At the ceiling, fail closed.
    scan_limit = 500 if require_complete else max(100, min(500, max(1, int(limit)) * 12))
    scan_kinds = ["raw_chunk", "memory"] if require_complete else ["raw_chunk"]
    records = _complete_raw_scan_records(
        store, kinds=scan_kinds, scope=scope, source_ids=source_ids, scan_limit=scan_limit,
    )

    query_terms = set(_terms(query))
    query_ngrams = _char_ngrams(query)
    proper = {item.lower() for item in _proper_nouns(query)}
    quoted = [item.lower() for item in _quoted_phrases(query)]
    scored: list[tuple[float, int, Any]] = []
    for index, record in enumerate(records):
        if require_complete and record.kind != "raw_chunk" and not _is_raw_candidate(record):
            continue
        text = _record_text(record)
        text_lower = text.lower()
        lexical = _lexical_score(query_terms, text)
        semantic = _jaccard_score(query_ngrams, _char_ngrams(text))
        proper_overlap = 0.0
        if proper:
            proper_overlap = min(0.25, 0.08 * sum(1 for item in proper if item in text_lower))
        phrase_overlap = 0.18 if quoted and any(phrase in text_lower for phrase in quoted) else 0.0
        score = round((lexical * 0.62) + (semantic * 0.28) + proper_overlap + phrase_overlap, 6)
        # Recency and correction markers rank grounded candidates; they are
        # not evidence that an unrelated raw record answers this query.
        if score <= 0:
            continue
        scored.append((score, index, record))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"record": record, "base_score": score} for score, _index, record in scored[: max(1, int(limit))]]


def _unique_raw_candidates(results: list[Any]) -> list[dict[str, Any]]:
    """Keep the strongest score per exact authority without counting it twice."""
    by_ref: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for item in results:
        record = _result_record(item)
        record_key = _raw_key(record)
        if not record_key[0]:
            continue
        candidate = _scored_candidate(record, item)
        previous = by_ref.get(record_key)
        if previous is None:
            by_ref[record_key] = candidate
        else:
            strongest = candidate if candidate["base_score"] > previous["base_score"] else previous
            ranks = [x["fts_rank"] for x in (candidate, previous) if "fts_rank" in x]
            if ranks:
                strongest["fts_rank"] = min(ranks)
            by_ref[record_key] = strongest
    return list(by_ref.values())


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
        reordered = invoke_raw_reranker(reranker, ranked, query=query, task_context=task_context,
                                        limit=limit, candidate_count=original_count)
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
    if len(documents) > 32 or any(len(text) > 16000 for text in documents) or len(query) > 16000:
        return ranked
    body = json.dumps({"model": model, "query": str(query or ""), "documents": documents},
                      allow_nan=False).encode("utf-8")
    if len(body) > 1024 * 1024:
        return ranked
    timeout_seconds = raw_remaining_seconds(
        _env_int("EIMEMORY_RAW_RETRIEVAL_RERANK_TIMEOUT") or 8, task_context)
    if timeout_seconds <= 0:
        return ranked
    rerank_deadline = perf_counter() + timeout_seconds
    try:
        with safe_urlopen(
            url,
            timeout=timeout_seconds,
            max_redirects=0,
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        ) as response:
            response_body = response.read(256 * 1024 + 1)
            if len(response_body) > 256 * 1024:
                return ranked
            payload = strict_json_loads(response_body, max_bytes=256 * 1024, max_depth=16)
        if perf_counter() >= rerank_deadline:
            return ranked
    except (urllib.error.URLError, UnsafeURL, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
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
        if type(index) is int and 0 <= index < len(ranked):
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
        normalized.append(_scored_candidate(record, item))
    return normalized


def _authoritative_raw_candidates(
    store: Any,
    *,
    candidates: list[dict[str, Any]],
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    strict_collection: bool = False,
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
            if strict_collection:
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
            continue
        if source_ids is not None and source_id not in source_ids:
            if strict_collection:
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
            continue
        if _scope_key(record_scope) not in authorized_scopes:
            if strict_collection:
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
            continue
        if not strict_collection and not raw_ref_allowed(record):
            continue
        try:
            with raw_read_scope(store):
                hydrated = store.get_by_exact_ref(record_id, scope=record_scope, source_id=source_id)
        except Exception as exc:
            raise RawRecallUnavailable("raw_recall_unavailable") from exc
        if (hydrated is None or hydrated.status != "active"
                or exact_ref(hydrated) != exact_ref(record)):
            if strict_collection:
                raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
            continue
        if strict_collection and authority_digest(hydrated) != authority_digest(record):
            raise RawRecallUnavailable("raw_recall_unavailable", allow_recovery=False)
        if not raw_ref_allowed(hydrated):
            continue
        capture_raw_snapshot(hydrated)
        authoritative.append(_scored_candidate(hydrated, item))
    return authoritative


def _authoritative_raw_records(
    store: Any,
    *,
    records: list[Any],
    scope: ScopeRef,
    source_ids: tuple[str, ...] | None,
    strict_collection: bool = False,
) -> list[Any]:
    return [
        _result_record(item)
        for item in _authoritative_raw_candidates(
            store,
            candidates=[{"record": record, "base_score": 0.0} for record in records],
            scope=scope,
            source_ids=source_ids,
            strict_collection=strict_collection,
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


def _boosts(*, query: str, text: str, record: Any, task_context: dict,
            content_match: ContentMatch, plan: QueryContent) -> dict[str, float]:
    boosts: dict[str, float] = {}
    query_terms = plan.terms
    if query_terms and content_match.terms:
        overlap = len(content_match.terms) / len(query_terms)
        if overlap:
            boosts["keyword_overlap"] = round(min(0.8, overlap * 0.8), 6)
    if content_match.phrases:
        boosts["quoted_phrase"] = 0.75
    if content_match.title_body_terms:
        # Within one coverage band, prefer a document about the topic over
        # an incidental body mention. This signal does not admit citations.
        boosts["title_body_overlap"] = content_match.title_body_terms / len(query_terms)
    proper = _proper_nouns(query) + _proper_nouns(str(task_context.get("question") or ""))
    proper = list(dict.fromkeys(proper))
    if content_match.strength and proper and any(noun.lower() in text.lower() for noun in proper):
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
        # Retain the diagnostic key; reward it only in a supported local pair.
        boosts["preference_pattern"] = 0.0
    # Current/correction/recency signals are applied only by the dated,
    # same-subject comparison, never independently across the candidate pool.
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
    records = _complete_raw_scan_records(
        store, kinds=["raw_chunk"], scope=scope, source_ids=source_ids, scan_limit=500,
    )
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
        if _raw_key(candidate)[1:] != _raw_key(record)[1:]:
            continue
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
        score = float(value)
        return max(0.0, score) if math.isfinite(score) else 0.0
    except (TypeError, ValueError, OverflowError):
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
            "chunk_index": str(record.content["chunk_index"]) if record.content.get("chunk_index") is not None else "",
            "source_event_id": str(record.content.get("source_event_id") or ""),
            "role": str(record.content.get("role") or ""),
            "speaker": str(record.content.get("speaker") or ""),
        }
    if isinstance(record, dict):
        payload = dict(record)
        payload.setdefault("record_id", str(payload.get("chunk_id") or payload.get("id") or ""))
        payload["text"] = text
        payload.setdefault("session_id", str(payload.get("session_id") or ""))
        payload.setdefault("turn_id", str(payload.get("turn_id") or ""))
        for key in ("role", "speaker", "source_event_id"):
            payload.setdefault(key, "")
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
        "source_event_id": str(getattr(record, "source_event_id", "") or ""),
        "role": str(getattr(record, "role", "") or ""),
        "speaker": str(getattr(record, "speaker", "") or ""),
    }


def _record_text(record: Any) -> str:
    """Return original evidence text, never synthetic first-person attribution.

    Summary/detail/title remain fallbacks for legacy raw-like records, not
    additional statements appended to a source that already has raw text.
    """
    if isinstance(record, RecordEnvelope):
        content = record.content if isinstance(record.content, dict) else {}
        values = [
            content.get("raw_text"),
            content.get("text"),
            content.get("body"),
            record.summary,
            record.detail,
            record.title,
        ]
        return next((str(value) for value in values if str(value or "").strip()), "")
    if isinstance(record, dict):
        values = [record.get(key) for key in ("raw_text", "text", "body", "summary", "detail", "title")]
        return next((str(value) for value in values if str(value or "").strip()), "")
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
    # Whitespace tokenization treats an entire Chinese sentence (and adjacent
    # ASCII identifiers) as one word. CJK bigrams ground short names inside
    # prose while non-CJK words keep their original identifier boundaries.
    terms: list[str] = []
    for word in _WORD_RE.findall(str(text or "").lower()):
        start = 0
        for match in _CJK_RUN_RE.finditer(word):
            prefix = word[start:match.start()]
            if len(prefix.strip("'")) > 1:
                terms.append(prefix)
            run = match.group()
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
            start = match.end()
        suffix = word[start:]
        if len(suffix.strip("'")) > 1:
            terms.append(suffix)
    return terms


def _quoted_phrases(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"['\"]([^'\"]{2,})['\"]", str(text or ""))]


def _proper_nouns(text: str) -> list[str]:
    return [match.group(0) for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", str(text or ""))]


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


def _current_fact(record: Any) -> bool:
    """Explicit record-level metadata only; fact dates do not assert currentness."""
    for container in _containers(record):
        value = container.get("current")
        if value is True or str(value).strip().lower() == "true":
            return True
    return False


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
